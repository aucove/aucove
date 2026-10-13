# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Manifiesto de la plantilla y archivos obligatorios desde 02-convencion §2.2.

Uso::

    python tools/gen_template_manifest.py --spec-dir <spec> [--milestone H2] [--check]

Lee la tabla de 02-convencion §2.2 de ``<spec>/02-convencion.md`` y la expande
con ``tools/manifest_expand.toml``, tras comprobar que la tabla de expansión la
cubre: cada ítem entre acentos graves de la columna «Ruta» aparece una sola
vez, ``in_template`` coincide con la columna «Origen» (T, o la enmienda D-35 j),
cada ruta concreta coincide con su glob y ninguna se repite. Escribe:

- ``tests/fixtures/template-manifest.txt``: las rutas de la plantilla hasta el
  hito pedido, POSIX, ordenadas, una por línea y con LF (12-mvp REQ-MVP-4). Es la
  variante ``format: latex``, la que se publica (REQ-CONV-1); ``build(..., fmt="none")``
  da la del render ``none`` (01-arquitectura REQ-ARCH-100).
- ``src/aucove/_data/required-paths.json``: las rutas que exige
  ``conv.tree.missing``, con el mismo filtro por hito (00-marco D-36) y, en
  ``formats``, el filtro por formato: ruta -> formatos que la exigen (D-36 enmendada,
  D-43). Una ruta que no está en ``formats`` se exige con cualquier formato.

Sin ``--spec-dir`` usa ``AUCOVE_SPEC_DIR``. Con ``--check`` no escribe: sale
con 1 si los archivos versionados no son los regenerados. Solo usa la
biblioteca estándar.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TABLE = REPO_ROOT / "tools" / "manifest_expand.toml"
DEFAULT_MANIFEST = REPO_ROOT / "tests" / "fixtures" / "template-manifest.txt"
DEFAULT_REQUIRED = REPO_ROOT / "src" / "aucove" / "_data" / "required-paths.json"
SPEC_FILE = "02-convencion.md"
SPEC_ENV = "AUCOVE_SPEC_DIR"
D35 = "D-35"
# Valores de project.yaml: format (00-marco D-43); la plantilla publicada es la variante latex.
FORMATS: tuple[str, ...] = ("latex", "none")
TEMPLATE_FORMAT = "latex"

_MILESTONE = re.compile(r"H([1-9][0-9]*)")
_HEADING = re.compile(r"^### 2\.2\b.*$", re.MULTILINE)
_CODE = re.compile(r"`([^`]+)`")
_PAREN = re.compile(r"\([^)]*\)")


class ManifestError(ValueError):
    """La tabla de 02 §2.2 y la tabla de expansión no concuerdan, o alguna está mal formada."""


@dataclass(frozen=True)
class Row:
    """Fila de la tabla de 02 §2.2: ítems de la columna Ruta y letras de la columna Origen."""

    items: tuple[str, ...]
    origin: frozenset[str]


def milestone_key(value: object) -> int:
    """``'H2'`` -> 2; ``ManifestError`` si no es un hito."""
    if not isinstance(value, str) or not _MILESTONE.fullmatch(value):
        raise ManifestError(f"hito inválido: {value!r}")
    return int(value[1:])


def _split_row(line: str) -> list[str]:
    """Celdas de una fila de tabla Markdown; un ``|`` entre acentos graves no separa."""
    cells: list[str] = []
    current: list[str] = []
    in_code = False
    for ch in line.strip():
        if ch == "`":
            in_code = not in_code
        if ch == "|" and not in_code:
            cells.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    cells.append("".join(current).strip())
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return cells


def parse_table(table_md: str) -> list[Row]:
    """Filas de la tabla de 02 §2.2 (con encabezado «Ruta» y «Origen»)."""
    text = table_md.replace("\r\n", "\n").replace("\r", "\n")
    heading = _HEADING.search(text)
    if heading is not None:
        rest = text[heading.end() :]
        nxt = re.search(r"^#", rest, re.MULTILINE)
        text = rest[: nxt.start()] if nxt else rest
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        header = _split_row(line)
        if header and header[0] == "Ruta" and "Origen" in header:
            break
    else:
        raise ManifestError("no se encuentra la tabla de 02-convencion §2.2 (columnas Ruta y Origen)")
    origin_col = header.index("Origen")
    rows: list[Row] = []
    for line in lines[index + 2 :]:  # salta el encabezado y la fila |---|
        if not line.lstrip().startswith("|"):
            break
        cells = _split_row(line)
        if len(cells) != len(header):
            raise ManifestError(f"fila con {len(cells)} celdas en vez de {len(header)}: {line!r}")
        items = tuple(_CODE.findall(cells[0]))
        if not items:
            raise ManifestError(f"fila sin rutas entre acentos graves: {line!r}")
        origin = frozenset(tok.strip() for tok in _PAREN.sub("", cells[origin_col]).split(",") if tok.strip())
        rows.append(Row(items, origin))
    if not rows:
        raise ManifestError("la tabla de 02-convencion §2.2 no tiene filas")
    return rows


def glob_regex(pattern: str) -> re.Pattern[str]:
    """Glob de la spec a regex: ``**`` cruza carpetas, ``*`` no."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def _check_path(path: object) -> str:
    if (
        not isinstance(path, str)
        or not path
        or path.startswith("/")
        or path.endswith("/")
        or "\\" in path
        or any(part in ("", ".", "..") for part in path.split("/"))
    ):
        raise ManifestError(f"ruta inválida: {path!r}")
    return path


def _items(table: Mapping[str, object]) -> list[Mapping[str, object]]:
    items = table.get("item")
    if not isinstance(items, list) or not items:
        raise ManifestError("la tabla de expansión no tiene [[item]]")
    for item in items:
        if not isinstance(item, dict):
            raise ManifestError(f"ítem mal formado: {item!r}")
        if not isinstance(item.get("spec"), str) or not item["spec"]:
            raise ManifestError(f"ítem sin spec: {item!r}")
        for key in ("in_template", "required"):
            if not isinstance(item.get(key), bool):
                raise ManifestError(f"{item['spec']}: falta {key} (booleano)")
        paths = item.get("paths")
        if not isinstance(paths, list) or not all(isinstance(p, dict) for p in paths):
            raise ManifestError(f"{item['spec']}: paths debe ser una lista de tablas")
        for entry in paths:
            _check_path(entry.get("path"))
            milestone_key(entry.get("milestone"))
            if "require_dir" in entry:
                _check_path(entry["require_dir"])
            if not isinstance(entry.get("early", False), bool):
                raise ManifestError(f"{item['spec']}: early debe ser un booleano")
            entry_formats(item, entry)
        if "milestone" in item:
            milestone_key(item["milestone"])
        item_formats(item)
    return items


def item_formats(item: Mapping[str, object]) -> tuple[str, ...]:
    """Formatos del ítem en el orden de ``FORMATS``; todos si no tiene la clave ``formats``."""
    value = item.get("formats", list(FORMATS))
    if (
        not isinstance(value, list)
        or not value
        or len(set(value)) != len(value)
        or not all(isinstance(v, str) and v in FORMATS for v in value)
    ):
        raise ManifestError(f"{item.get('spec')}: formats debe ser una lista sin repetidos de {list(FORMATS)}")
    return tuple(f for f in FORMATS if f in value)


def entry_formats(item: Mapping[str, object], entry: Mapping[str, object]) -> tuple[str, ...]:
    """Formatos de una ruta: los suyos (``formats`` de la entrada, un subconjunto de los del
    ítem; así los workflows del editor del artículo solo van con ``latex``) o los del ítem."""
    formats = item_formats(item)
    if "formats" not in entry:
        return formats
    own = item_formats({"spec": item.get("spec"), "formats": entry["formats"]})
    if not set(own) <= set(formats):
        raise ManifestError(f"{item.get('spec')}: los formats de {entry.get('path')} no son del ítem")
    return own


def in_render(item: Mapping[str, object], fmt: str, entry: Mapping[str, object] | None = None) -> bool:
    """¿Las rutas del ítem (o la ruta ``entry``) están en el render de ``fmt``?

    ``in_template`` habla de la plantilla publicada, la variante ``latex`` (REQ-CONV-1); un
    ítem que no vale para ``latex`` está en el render de sus formatos (REQ-ARCH-100).
    """
    formats = item_formats(item)
    own = formats if entry is None else entry_formats(item, entry)
    return fmt in own and (bool(item["in_template"]) or TEMPLATE_FORMAT not in formats)


def delivered(entry: Mapping[str, object], target: int) -> bool:
    """La ruta ya está en la plantilla del hito ``target``: el suyo no es posterior o ``early``
    dice que llegó antes que el resto de su hito (se quita al pasar la tabla a ese hito)."""
    return milestone_key(entry["milestone"]) <= target or entry.get("early") is True


def _sections(table: Mapping[str, object]) -> list[str]:
    """Partes de la plantilla con que se expande ``<id>`` (00-marco D-55: las 7 por defecto)."""
    raw = table.get("sections_example", ["introduction"])
    ok = isinstance(raw, list) and raw and len(set(raw)) == len(raw) and all(
        isinstance(s, str) and re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", s) for s in raw
    )
    if not ok:
        raise ManifestError(f"sections_example inválido: {raw!r}")
    return list(raw)


def check_table(rows: Sequence[Row], table: Mapping[str, object]) -> None:
    """Comprueba que la tabla de expansión cubre la de 02 §2.2; ``ManifestError`` con todos los problemas."""
    items = _items(table)
    sections = _sections(table)
    problems: list[str] = []
    origin: dict[str, frozenset[str]] = {}
    for row in rows:
        for name in row.items:
            if name in origin:
                problems.append(f"{name}: aparece más de una vez en 02 §2.2")
            origin[name] = row.origin
    seen: set[str] = set()
    owner: dict[str, str] = {}
    for item in items:
        spec = str(item["spec"])
        if spec in seen:
            problems.append(f"{spec}: aparece más de una vez en la tabla de expansión")
        seen.add(spec)
        if spec not in origin:
            problems.append(f"{spec}: no está en la columna Ruta de 02 §2.2")
            continue
        in_template = bool(item["in_template"])
        amendment = item.get("amendment")
        has_t = "T" in origin[spec]
        if amendment is not None and amendment != D35:
            problems.append(f"{spec}: enmienda desconocida {amendment!r}")
        if amendment == D35 and has_t:
            problems.append(f"{spec}: D-35 solo aplica a rutas sin origen T")
        expected = has_t or amendment == D35
        if in_template != expected:
            problems.append(f"{spec}: in_template={in_template}, pero el origen es {sorted(origin[spec])}")
        paths = item["paths"]
        assert isinstance(paths, list)
        formats = item_formats(item)
        other_format = TEMPLATE_FORMAT not in formats  # solo existe en el render de otro formato
        if in_template and other_format:
            problems.append(f"{spec}: la plantilla publicada es la variante {TEMPLATE_FORMAT} y el ítem no vale para ella")
        if not in_template and not other_format and (paths or item["required"]):
            problems.append(f"{spec}: no viene en la plantilla, así que no lleva rutas ni es obligatorio")
        if other_format and not in_template and item["required"] and not paths:
            problems.append(f"{spec}: es obligatorio con {list(formats)}, así que necesita sus rutas")
        if in_template and not paths and "milestone" not in item:
            problems.append(f"{spec}: viene en la plantilla sin rutas y sin el hito que las traerá")
        glob = item.get("glob", spec)
        if not isinstance(glob, str):
            problems.append(f"{spec}: glob inválido")
            continue
        for entry in paths:
            path = str(entry["path"])
            for section in sections if "<id>" in path else sections[:1]:
                pattern = glob_regex(glob.replace("<id>", section))
                concrete = path.replace("<id>", section)
                if not pattern.fullmatch(concrete):
                    problems.append(f"{spec}: {path} no coincide con {glob}")
                if concrete in owner:
                    problems.append(f"{path}: repetida en {owner[concrete]} y {spec}")
                owner[concrete] = spec
            require_dir = entry.get("require_dir")
            if require_dir is not None:
                if not path.startswith(f"{require_dir}/") or "<id>" not in str(require_dir):
                    problems.append(f"{spec}: require_dir {require_dir} debe ser una carpeta de la parte de {path}")
    for name in origin:
        if name not in seen:
            problems.append(f"{name}: falta en la tabla de expansión")
    if problems:
        raise ManifestError("; ".join(problems))


def build(
    table: Mapping[str, object], milestone: str, fmt: str = TEMPLATE_FORMAT
) -> tuple[list[str], dict[str, object]]:
    """Manifiesto del render ``fmt`` y ``required-paths`` hasta ``milestone``, sin mirar la spec.

    ``required-paths`` no depende de ``fmt``: lleva las rutas de todos los formatos y, en
    ``formats``, las que solo se exigen con algunos (D-36 enmendada).
    """
    if fmt not in FORMATS:
        raise ManifestError(f"formato inválido: {fmt!r}")
    target = milestone_key(milestone)
    sections = _sections(table)
    manifest: set[str] = set()
    fixed: set[str] = set()
    per_section: list[str] = []
    per_section_dirs: list[str] = []
    by_format: dict[str, list[str]] = {}
    for item in _items(table):
        paths = item["paths"]
        assert isinstance(paths, list)
        if item["in_template"] and not paths and milestone_key(item.get("milestone", "H1")) <= target:
            raise ManifestError(f"{item['spec']}: el hito {milestone} necesita sus rutas concretas")
        for entry in paths:
            if not delivered(entry, target):
                continue
            formats = entry_formats(item, entry)
            path = str(entry["path"])
            if in_render(item, fmt, entry):
                manifest.update(path.replace("<id>", s) for s in sections)
            if not item["required"]:
                continue
            require_dir = entry.get("require_dir")
            if require_dir is not None:
                value = str(require_dir).replace("<id>", "{id}")
                if value not in per_section_dirs:
                    per_section_dirs.append(value)
            elif "<id>" in path:
                value = path.replace("<id>", "{id}")
                if value not in per_section:
                    per_section.append(value)
            else:
                value = path
                fixed.add(path)
            if formats != FORMATS:
                by_format[value] = list(formats)
    required: dict[str, object] = {
        "milestone": milestone,
        "fixed": sorted(fixed),
        "per_section": per_section,
        "per_section_dirs": per_section_dirs,
        "formats": {path: by_format[path] for path in sorted(by_format)},
    }
    return sorted(manifest), required


def expand(table_md: str, table: Mapping[str, object], milestone: str) -> tuple[list[str], dict[str, object]]:
    """(manifiesto, required-paths) de ``milestone``, tras comprobar la tabla de expansión contra 02 §2.2."""
    check_table(parse_table(table_md), table)
    return build(table, milestone)


def manifest_bytes(paths: Sequence[str]) -> bytes:
    return "".join(f"{path}\n" for path in paths).encode("utf-8")


def required_bytes(required: Mapping[str, object]) -> bytes:
    return (json.dumps(required, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def load_table(path: Path) -> dict[str, object]:
    try:
        return tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"no se puede leer {path}: {exc}") from exc


def outputs(spec_dir: Path, table: Mapping[str, object], milestone: str) -> tuple[bytes, bytes]:
    """Bytes del manifiesto y de ``required-paths.json`` regenerados desde la spec."""
    spec_path = Path(spec_dir) / SPEC_FILE
    try:
        table_md = spec_path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"no se puede leer {spec_path}: {exc}") from exc
    manifest, required = expand(table_md, table, milestone)
    return manifest_bytes(manifest), required_bytes(required)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Genera el manifiesto de la plantilla y required-paths.json.")
    parser.add_argument("--spec-dir", type=Path, default=None, help=f"carpeta de la spec (por defecto ${SPEC_ENV})")
    parser.add_argument("--milestone", default=None, help="hito (por defecto, el de la tabla de expansión)")
    parser.add_argument("--table", type=Path, default=DEFAULT_TABLE, help=argparse.SUPPRESS)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help=argparse.SUPPRESS)
    parser.add_argument("--required", type=Path, default=DEFAULT_REQUIRED, help=argparse.SUPPRESS)
    parser.add_argument("--check", action="store_true", help="solo comprueba; sale con 1 si hay diferencias")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 1
    spec_dir = args.spec_dir or (Path(os.environ[SPEC_ENV]) if os.environ.get(SPEC_ENV) else None)
    if spec_dir is None:
        print(f"error: falta --spec-dir (o {SPEC_ENV})", file=sys.stderr)
        return 1
    try:
        table = load_table(args.table)
        milestone = args.milestone or str(table.get("milestone", ""))
        milestone_key(milestone)
        manifest, required = outputs(spec_dir, table, milestone)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    targets = ((args.manifest, manifest), (args.required, required))
    if args.check:
        stale = [path for path, data in targets if not path.is_file() or path.read_bytes() != data]
        for path in stale:
            print(f"desactualizado: {path}", file=sys.stderr)
        return 1 if stale else 0
    try:
        for path, data in targets:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            print(path)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
