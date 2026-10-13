# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Doble de pruebas de la CLI de gitleaks (``gitleaks git``) para la regla ``secrets``.

Emula la parte de gitleaks 8 que usa la regla (10-seguridad REQ-SEC-36)::

    fake_gitleaks.py git --log-opts A..B --redact --config F --report-format json \\
        --report-path P --exit-code N --no-banner --ignore-gitleaks-allow SOURCE

- Recorre las líneas agregadas por los commits del rango con
  ``git -C SOURCE log -p -U0 <log-opts>``, como gitleaks: el valor de ``--log-opts``
  se separa por espacios y se agrega tal cual, sin otras opciones de diff, así que un
  archivo que git trata como binario solo aparece como "Binary files ... differ" salvo
  que la regla pase ``--text``, y un commit de fusión no muestra diff salvo que pase
  ``--diff-merges``. Reporta cada aparición del patrón de prueba
  ``FAKELEAK`` seguido de 16 dígitos (no lo detectan las reglas reales de gitleaks).
  No modela la lista de permitidos global de la configuración por defecto.
- Escribe el reporte JSON (lista de hallazgos con los campos de gitleaks; ``[]`` si no
  hay) y sale con 0 sin hallazgos o con el valor de ``--exit-code`` (por defecto 1) si
  los hay. Ante un argumento inválido, una configuración ilegible o un fallo de git,
  sale con 1 sin reporte, como gitleaks ante un error.

Variables de control (las fijan las pruebas):

- ``FAKE_GITLEAKS_LOG``: archivo donde se guarda un JSON con ``argv``, ``cwd`` y las
  variables ``GITLEAKS*`` que recibió el proceso.
- ``FAKE_GITLEAKS_EXIT``: código de salida forzado; no escribe reporte salvo que se
  pida con ``FAKE_GITLEAKS_REPORT``.
- ``FAKE_GITLEAKS_REPORT``: ``none`` (no escribe reporte), ``garbage`` (texto que no
  es JSON) o un JSON literal que se escribe tal cual en lugar del calculado.
- ``FAKE_GITLEAKS_SLEEP``: segundos de espera antes de terminar.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

PATTERN = re.compile(r"FAKELEAK[0-9]{16}")
RULE_ID = "fake-leak"
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _parse(argv: list[str]) -> dict[str, object] | None:
    """Opciones de ``gitleaks git``; ``None`` si la línea no es válida."""
    if not argv or argv[0] != "git":
        return None
    opts: dict[str, object] = {"exit_code": 1, "redact": False, "source": None}
    with_value = {
        "--log-opts": "log_opts",
        "--config": "config",
        "--report-format": "report_format",
        "--report-path": "report_path",
        "--exit-code": "exit_code",
    }
    flags = {"--redact": "redact", "--no-banner": "no_banner", "--ignore-gitleaks-allow": "ignore_allow"}
    i = 1
    while i < len(argv):
        arg = argv[i]
        name, eq, value = arg.partition("=")
        if name in with_value:
            if not eq:
                i += 1
                if i >= len(argv):
                    return None
                value = argv[i]
            opts[with_value[name]] = value
        elif arg in flags:
            opts[flags[arg]] = True
        elif arg.startswith("-"):
            return None  # opción desconocida: gitleaks la rechaza
        elif opts["source"] is None:
            opts["source"] = arg
        else:
            return None
        i += 1
    try:
        opts["exit_code"] = int(str(opts["exit_code"]))
    except ValueError:
        return None
    return opts


def _scan(source: str, log_opts: str, redact: bool) -> list[dict[str, object]] | None:
    """Hallazgos de las líneas agregadas en el rango; ``None`` si git falla."""
    git = shutil.which("git")
    if git is None:
        return None
    # gitleaks: git -C SOURCE log -p -U0 <log-opts separadas por espacios>; el formato
    # y --no-color solo sirven para que este doble lea la salida.
    cmd = [git, "-C", source, "log", "-p", "-U0", "--no-color", "--format=%x00%H",
           *(opt for opt in log_opts.split(" ") if opt), "--"]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        return None
    leaks: list[dict[str, object]] = []
    commit = ""
    path = ""
    line_no = 0
    for raw in proc.stdout.decode("utf-8", "replace").split("\n"):
        if raw.startswith("\0"):
            commit, path, line_no = raw.strip("\0").strip(), "", 0
            continue
        if raw.startswith("+++ "):
            target = raw[4:]
            path = target[2:] if target.startswith("b/") else ""
            continue
        hunk = _HUNK.match(raw)
        if hunk:
            line_no = int(hunk.group(1))
            continue
        if raw.startswith("+") and path:
            text = raw[1:]
            for m in PATTERN.finditer(text):
                secret = m.group(0)
                leaks.append({
                    "Description": "Fake leak used by the tests",
                    "StartLine": line_no,
                    "EndLine": line_no,
                    "StartColumn": m.start() + 1,
                    "EndColumn": m.end(),
                    "Match": text.replace(secret, "REDACTED") if redact else text,
                    "Secret": "REDACTED" if redact else secret,
                    "File": path,
                    "SymlinkFile": "",
                    "Commit": commit,
                    "Entropy": 0,
                    "Author": "",
                    "Email": "",
                    "Date": "",
                    "Message": "",
                    "Tags": [],
                    "RuleID": RULE_ID,
                    "Fingerprint": f"{commit}:{path}:{RULE_ID}:{line_no}",
                })
            line_no += 1
    return leaks


def main(argv: list[str]) -> int:
    log = os.environ.get("FAKE_GITLEAKS_LOG")
    if log:
        env = {k: v for k, v in os.environ.items() if k.upper().startswith("GITLEAKS")}
        Path(log).write_text(json.dumps({"argv": argv, "cwd": os.getcwd(), "env": env}), encoding="utf-8")
    sleep = os.environ.get("FAKE_GITLEAKS_SLEEP")
    if sleep:
        time.sleep(float(sleep))
    opts = _parse(argv)
    if opts is None or opts.get("report_format") != "json" or not opts.get("report_path"):
        print("fake gitleaks: invalid arguments", file=sys.stderr)
        return 1
    report = Path(str(opts["report_path"]))
    forced_report = os.environ.get("FAKE_GITLEAKS_REPORT")
    forced_exit = os.environ.get("FAKE_GITLEAKS_EXIT")
    if forced_exit is not None:
        if forced_report not in (None, "none"):
            report.write_text("not json" if forced_report == "garbage" else forced_report, encoding="utf-8")
        return int(forced_exit)
    config = opts.get("config")
    if not config or not Path(str(config)).is_file():
        print("fake gitleaks: unreadable config", file=sys.stderr)
        return 1
    leaks = _scan(str(opts["source"] or "."), str(opts.get("log_opts") or ""), bool(opts["redact"]))
    if leaks is None:
        print("fake gitleaks: git failed", file=sys.stderr)
        return 1
    if forced_report == "garbage":
        report.write_text("not json", encoding="utf-8")
    elif forced_report not in (None, "none"):
        report.write_text(forced_report, encoding="utf-8")
    elif forced_report is None:
        report.write_text(json.dumps(leaks, indent=1), encoding="utf-8")
    return int(opts["exit_code"]) if leaks else 0  # type: ignore[call-overload]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
