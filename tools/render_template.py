# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Render de ``template/`` más los generados en un directorio (01-arquitectura §2.3, §10.5).

Uso::

    python tools/render_template.py --lang es|en --out DIR [--vars tools/template_vars.toml]
                                    [--set clave=valor]... [--core-pin X.Y.Z]
                                    [--format latex|none] [--check]

- Toma los valores de ``{{var:...}}`` de ``tools/template_vars.toml``, les
  aplica cada ``--set`` y fija ``language`` = ``--lang``. ``--core-pin X.Y.Z`` (forma
  del job ``build`` de 01 §2.3) fija ``core_pin`` y deriva ``core`` como lo escribe
  ``init`` (REQ-ARCH-44), para que el pin, aunque sea una versión preliminar, cumpla el
  rango (REQ-SCHEMA-83); un ``--set core=...`` explícito manda.
- ``--format`` elige la variante (REQ-ARCH-100, D-43): ``latex``, por defecto, es la
  plantilla publicada; ``none`` es la de un proyecto sin manuscrito, la que ``init
  --format none`` usará en H3.
- El render es el del núcleo, ``aucove.render.project.render_project`` (01 REQ-ARCH-102), el
  mismo que usa ``init --repo`` (00-marco D-73): ``base/`` + ``l10n/<lang>/`` (con las
  variantes de ``formats/<formato>/l10n/<lang>/``, como la constitución del render
  ``none``), ``.github/CODEOWNERS`` y ``.github/ISSUE_TEMPLATE/task.yml`` desde el
  ``roles.yaml`` renderizado (D-35 j), y ``.mcp.json`` (``render.mcp``, desde H3).
- Sin ``--check``, escribe en ``--out``, que debe no existir o estar vacío.
  Con ``--check``, no escribe: compara el render con ``--out`` (sin su ``.git``),
  con los archivos de texto de ``--out`` pasados de CRLF a LF (REQ-ARCH-11), y
  sale con 1 si hay alguna diferencia.

Salida: 0 si todo va bien; 1 ante una diferencia o un error.
"""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

from aucove.domain.compat import core_range
from aucove.i18n import LANGS, Lang
from aucove.render.project import ROLES_PATH, generated_from_roles, render_project
from aucove.render.template import FORMATS, RenderError, is_text

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TEMPLATE_DIR = REPO_ROOT / "template"
DEFAULT_VARS = REPO_ROOT / "tools" / "template_vars.toml"
__all__ = ["ROLES_PATH", "compare", "generated_from_roles", "load_vars", "main", "render_files", "render_to"]


def load_vars(path: Path) -> dict[str, str]:
    """Valores de ``template_vars.toml``; ``RenderError`` si no es TOML o algún valor no es texto."""
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RenderError(f"no se puede leer {path}: {exc}") from exc
    out: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(value, str):
            raise RenderError(f"{path}: el valor de {key!r} no es texto")
        out[key] = value
    return out


def render_files(
    lang: Lang,
    *,
    variables: Mapping[str, str] | None = None,
    template_dir: Path | None = None,
    fmt: str = "latex",
) -> dict[str, bytes]:
    """Ruta -> bytes del render completo (plantilla + generados) de ``format: fmt``, sin escribir nada."""
    src = DEFAULT_TEMPLATE_DIR if template_dir is None else Path(template_dir)
    values = load_vars(DEFAULT_VARS) if variables is None else variables
    return render_project(lang, values, template_dir=src, fmt=fmt)


def render_to(
    out: Path,
    lang: Lang,
    *,
    variables: Mapping[str, str] | None = None,
    template_dir: Path | None = None,
    fmt: str = "latex",
) -> list[str]:
    """Escribe el render en ``out`` (que no existe o está vacío); devuelve las rutas escritas, ordenadas."""
    out = Path(out)
    files = render_files(lang, variables=variables, template_dir=template_dir, fmt=fmt)
    if out.exists():
        if not out.is_dir():
            raise RenderError(f"{out} no es un directorio")
        if any(out.iterdir()):
            raise RenderError(f"{out} no está vacío")
    for rel, data in files.items():
        target = out.joinpath(*rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return sorted(files)


def _existing(out: Path) -> dict[str, Path]:
    """Archivos (y enlaces) bajo ``out``, sin entrar en su ``.git`` ni seguir enlaces."""
    found: dict[str, Path] = {}
    for dirpath, dirnames, filenames in os.walk(out, followlinks=False):
        current = Path(dirpath)
        if current == out:  # .git puede ser carpeta o, en un worktree, archivo
            dirnames[:] = [name for name in dirnames if name != ".git"]
            filenames = [name for name in filenames if name != ".git"]
        for name in list(dirnames):
            if os.path.islink(current / name):
                dirnames.remove(name)
                filenames.append(name)
        for name in filenames:
            path = current / name
            found[path.relative_to(out).as_posix()] = path
    return found


def compare(out: Path, files: Mapping[str, bytes]) -> list[str]:
    """Diferencias entre ``files`` y el árbol ``out`` (vacío = idénticos)."""
    out = Path(out)
    if not out.is_dir():
        return [f"no existe el directorio {out}"]
    present = _existing(out)
    problems: list[str] = []
    for rel in sorted(set(present) - set(files)):
        problems.append(f"sobra: {rel}")
    for rel, expected in files.items():
        path = present.get(rel)
        if path is None:
            problems.append(f"falta: {rel}")
            continue
        if path.is_symlink():
            problems.append(f"es un enlace: {rel}")
            continue
        actual = path.read_bytes()
        if is_text(actual):
            actual = actual.replace(b"\r\n", b"\n")
        if actual != expected:
            problems.append(f"difiere: {rel}")
    return problems


def _parse_set(items: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise RenderError(f"--set espera clave=valor: {item!r}")
        out[key.strip()] = value
    return out


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Renderiza template/ y los archivos generados en un directorio.")
    parser.add_argument("--lang", required=True, choices=LANGS, help="idioma del render (fija language)")
    parser.add_argument("--out", required=True, type=Path, help="directorio de salida (o a comparar con --check)")
    parser.add_argument("--vars", type=Path, default=DEFAULT_VARS, help="valores de {{var:...}} (TOML)")
    parser.add_argument("--set", action="append", default=[], metavar="CLAVE=VALOR", help="reemplaza una variable")
    parser.add_argument("--core-pin", help="fija core_pin=X.Y.Z y deriva core (REQ-ARCH-44), salvo --set core=...")
    parser.add_argument(
        "--format", dest="fmt", choices=FORMATS, default="latex",
        help="variante del render: latex (la plantilla publicada) o none (sin manuscrito)",
    )
    parser.add_argument("--template-dir", type=Path, default=DEFAULT_TEMPLATE_DIR, help=argparse.SUPPRESS)
    parser.add_argument("--check", action="store_true", help="no escribe: sale con 1 si --out difiere del render")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # argparse: --help sale con 0; un error de uso, con 1 (no 2)
        return 0 if exc.code in (0, None) else 1
    try:
        variables = load_vars(args.vars)
        overrides = _parse_set(args.set)
        variables.update(overrides)
        if args.core_pin is not None:
            try:
                derived = core_range(args.core_pin)
            except ValueError as exc:
                raise RenderError(f"--core-pin: {exc}") from exc
            variables["core_pin"] = args.core_pin
            if "core" not in overrides:
                variables["core"] = derived
        if args.check:
            files = render_files(args.lang, variables=variables, template_dir=args.template_dir, fmt=args.fmt)
            problems = compare(args.out, files)
            for problem in problems:
                print(problem, file=sys.stderr)
            return 1 if problems else 0
        for rel in render_to(args.out, args.lang, variables=variables, template_dir=args.template_dir, fmt=args.fmt):
            print(rel)
    except (OSError, RenderError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
