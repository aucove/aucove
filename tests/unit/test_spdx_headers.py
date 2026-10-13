# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Cabeceras SPDX (11-trazabilidad REQ-TRACE-56) en cada .py de src/, tools/ y tests/.

11-trazabilidad REQ-TRACE-56 pide cabeceras de licencia y de copyright en cada
archivo fuente. Esta prueba recorre el árbol real y lista los archivos que no
empiezan con las dos líneas SPDX, con el motivo de cada uno.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNED_DIRS: tuple[str, ...] = ("src", "tools", "tests")
SKIPPED_DIRS: frozenset[str] = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis"}
)

# Las etiquetas se arman por concatenación para que reuse no las lea como
# información de licencia de este archivo.
_TAG = "SPDX" + "-"
HOLDER = "2026 The aucove Authors"
HEADER_LINES: tuple[str, str] = (
    f"# {_TAG}FileCopyrightText: {HOLDER}",
    f"# {_TAG}License-Identifier: Apache-2.0",
)
_BOM = b"\xef\xbb\xbf"


def iter_python_files(root: Path, dirs: tuple[str, ...] = SCANNED_DIRS) -> Iterator[Path]:
    """Recorre los .py de cada directorio de ``dirs`` bajo ``root``, en orden estable."""
    for name in dirs:
        base = root / name
        if not base.is_dir():
            continue
        for current, subdirs, files in os.walk(base):
            # Poda en el sitio para que os.walk no entre en cachés.
            subdirs[:] = sorted(d for d in subdirs if d not in SKIPPED_DIRS)
            for file_name in sorted(files):
                if file_name.endswith(".py"):
                    yield Path(current) / file_name


def header_problem(path: Path) -> str | None:
    """Devuelve el motivo por el que ``path`` no cumple la regla, o None si cumple.

    Se admite una línea ``#!`` antes de la cabecera, porque un shebang solo
    funciona en la primera línea.
    """
    data = path.read_bytes()
    if data.startswith(_BOM):
        return "empieza con BOM"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "no es UTF-8"
    if "\r" in text:
        return "tiene fines de línea CR o CRLF"
    lines = text.split("\n")
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
    head = tuple(lines[:2])
    if head == HEADER_LINES:
        return None
    if not any(_TAG in line for line in lines[:3]):
        return "falta la cabecera SPDX"
    return "la cabecera SPDX no es la del repositorio"


def missing_headers(root: Path, dirs: tuple[str, ...] = SCANNED_DIRS) -> list[str]:
    """Lista ``ruta: motivo`` (rutas relativas con '/') de los .py que no cumplen la regla."""
    problems: list[str] = []
    for path in iter_python_files(root, dirs):
        reason = header_problem(path)
        if reason is not None:
            problems.append(f"{path.relative_to(root).as_posix()}: {reason}")
    return problems


def _header() -> str:
    return "\n".join(HEADER_LINES) + "\n"


def test_repo_python_files_have_c27_header() -> None:
    problems = missing_headers(REPO_ROOT)
    assert problems == [], "Archivos .py sin la cabecera SPDX del repositorio:\n" + "\n".join(problems)


def test_this_file_is_scanned() -> None:
    # Si el recorrido no encontrara este archivo, la prueba anterior pasaría en vacío.
    scanned = {p.resolve() for p in iter_python_files(REPO_ROOT)}
    assert Path(__file__).resolve() in scanned


def test_header_problem_accepts_valid_files(tmp_path: Path) -> None:
    plain = tmp_path / "plain.py"
    plain.write_bytes((_header() + '"""Docstring."""\n').encode("utf-8"))
    only_header = tmp_path / "only_header.py"
    only_header.write_bytes(_header().encode("utf-8"))
    shebang = tmp_path / "script.py"
    shebang.write_bytes(("#!/usr/bin/env python3\n" + _header() + "print('hola')\n").encode("utf-8"))
    for path in (plain, only_header, shebang):
        assert header_problem(path) is None, path.name


def test_header_problem_reports_each_defect(tmp_path: Path) -> None:
    cases: dict[str, bytes] = {
        "empty.py": b"",
        "no_header.py": b"x = 1\n",
        "bom.py": _BOM + _header().encode("utf-8"),
        "crlf.py": _header().replace("\n", "\r\n").encode("utf-8"),
        "latin1.py": "# comentario en latín-1: año\n".encode("latin-1"),
        "other_license.py": (HEADER_LINES[0] + f"\n# {_TAG}License-Identifier: MIT\n").encode("utf-8"),
        "swapped.py": (HEADER_LINES[1] + "\n" + HEADER_LINES[0] + "\n").encode("utf-8"),
        "late.py": ("x = 1\n" + _header()).encode("utf-8"),
        "other_holder.py": (
            f"# {_TAG}FileCopyrightText: 2026 Otra Institución\n" + HEADER_LINES[1] + "\n"
        ).encode("utf-8"),
    }
    expected: dict[str, str] = {
        "empty.py": "falta la cabecera SPDX",
        "no_header.py": "falta la cabecera SPDX",
        "bom.py": "empieza con BOM",
        "crlf.py": "tiene fines de línea CR o CRLF",
        "latin1.py": "no es UTF-8",
        "other_license.py": "la cabecera SPDX no es la del repositorio",
        "swapped.py": "la cabecera SPDX no es la del repositorio",
        "late.py": "la cabecera SPDX no es la del repositorio",
        "other_holder.py": "la cabecera SPDX no es la del repositorio",
    }
    for name, data in cases.items():
        (tmp_path / name).write_bytes(data)
    for name, reason in expected.items():
        assert header_problem(tmp_path / name) == reason, name


def test_missing_headers_walks_only_scanned_dirs(tmp_path: Path) -> None:
    good = _header().encode("utf-8")
    files: dict[str, bytes] = {
        "src/pkg/__init__.py": b"",
        "src/pkg/mod.py": good + b"x = 1\n",
        "src/pkg/__pycache__/mod.py": b"",
        "tools/build.py": b"print('sin cabecera')\n",
        "tests/unit/test_x.py": good,
        "tests/fixtures/tools/fake_tool.py": b"import sys\n",
        "tests/fixtures/data.txt": b"no es python\n",
        "template/base/sections/intro/analysis/run.py": b"x = 1\n",
        "other/elsewhere.py": b"",
    }
    for rel, data in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    assert missing_headers(tmp_path) == [
        "src/pkg/__init__.py: falta la cabecera SPDX",
        "tools/build.py: falta la cabecera SPDX",
        "tests/fixtures/tools/fake_tool.py: falta la cabecera SPDX",
    ]


def test_missing_headers_tolerates_absent_dirs(tmp_path: Path) -> None:
    assert missing_headers(tmp_path) == []
