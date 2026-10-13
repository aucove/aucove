# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Pasos de ``release.yml`` que no son comandos de uv (01-arquitectura §2.3; 10-seguridad §6.2).

Uso::

    python tools/release.py check-tag --tag vX.Y.Z
    python tools/release.py sbom-version --file assets/sbom.cdx.json --version X.Y.Z
    python tools/release.py notes --version X.Y.Z --repo OWNER/REPO [--previous vA.B.C]
    python tools/release.py pypi-readme --repo OWNER/REPO --ref vX.Y.Z [--file README.md]

- ``check-tag``: la etiqueta que dispara el release DEBE ser ``v`` más la versión del
  paquete (``[tool.hatch.version] path`` de ``pyproject.toml``), con la forma de 00-marco
  D-66. Imprime ``version=`` y ``prerelease=`` para ``$GITHUB_OUTPUT``: una versión
  preliminar (``aN``, ``bN``, ``rcN``) no actualiza la plantilla (REQ-ARCH-8).
- ``sbom-version``: ``uv export --format cyclonedx1.5`` deja sin versión el componente raíz,
  porque la versión es dinámica (01 §2.3). Le pone la versión y su ``purl`` de PyPI.
- ``notes``: las notas de la GitHub Release salen de la sección ``## [X.Y.Z]`` de
  ``CHANGELOG.md``. Si no existe o está vacía, falla: se escribe antes de etiquetar
  (guía de mantenimiento, punto 10). Agrega cómo verificar los archivos (REQ-SEC-52) y, en
  una versión final con ``--previous``, el enlace a los cambios de la plantilla.
- ``pypi-readme``: el README es la descripción del paquete en PyPI, donde sus rutas relativas
  al repo no llevan a ningún lado. Antes de ``uv build`` las reescribe como URL absolutas a la
  etiqueta: las imágenes a ``raw.githubusercontent.com``, los archivos a ``blob/`` y las
  carpetas a ``tree/``. Solo cambia la copia del job; el README del repo queda igual.

Solo usa la biblioteca estándar, para correr en los jobs del release sin instalar nada.
Sale con 0 si todo va bien y con 1 ante cualquier error, que va a stderr.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?")  # 00-marco D-66
PRERELEASE_RE = re.compile(r"(?:a|b|rc)[0-9]+$")
_DUNDER_VERSION = re.compile(r'^__version__ = "([^"]+)"', re.M)
_SECTION = re.compile(r"^## \[(?P<name>[^\]]+)\]", re.M)
REPO_RE = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")
# Un atributo src= o href= de HTML, o un enlace o una imagen de Markdown: [texto](ruta), ![alt](ruta).
_LINK = re.compile(r'(?P<attr>\b(?:src|href)=")(?P<html>[^"]+)(?=")'
                   r"|(?P<bang>!?)\[(?P<label>[^\]]*)\]\((?P<md>[^)\s]+)\)")
_FENCE = re.compile(r"^```.*?^```[^\n]*$", re.M | re.S)
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")


class ReleaseError(Exception):
    """Un paso del release que no puede seguir; el mensaje dice por qué."""


def _project(root: Path) -> dict[str, object]:
    return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))


def project_name(root: Path = ROOT) -> str:
    return str(_project(root)["project"]["name"])  # type: ignore[index]


def package_version(root: Path = ROOT) -> str:
    """``__version__`` del archivo que lee hatch (``[tool.hatch.version] path``)."""
    path = root / str(_project(root)["tool"]["hatch"]["version"]["path"])  # type: ignore[index]
    found = _DUNDER_VERSION.search(path.read_text(encoding="utf-8"))
    if found is None:
        raise ReleaseError(f"{path.relative_to(root).as_posix()} no tiene una línea __version__")
    return found.group(1)


def is_prerelease(version: str) -> bool:
    return PRERELEASE_RE.search(version) is not None


def check_tag(tag: str, version: str) -> tuple[str, bool]:
    """``(versión, preliminar)`` si ``tag`` es ``v<versión>`` y la versión tiene la forma de D-66."""
    if not VERSION_RE.fullmatch(version):
        raise ReleaseError(f"la versión del paquete, {version!r}, no tiene la forma X.Y.Z[aN|bN|rcN]")
    if tag != f"v{version}":
        raise ReleaseError(f"la etiqueta {tag!r} no es la versión del paquete (v{version})")
    return version, is_prerelease(version)


def set_sbom_version(doc: dict[str, object], name: str, version: str) -> dict[str, object]:
    """El componente raíz de un SBOM CycloneDX con su versión y su ``purl`` de PyPI."""
    metadata = doc.get("metadata")
    component = metadata.get("component") if isinstance(metadata, dict) else None
    if not isinstance(component, dict):
        raise ReleaseError("el SBOM no tiene metadata.component")
    found = str(component.get("name", ""))
    if found.lower().replace("_", "-") != name.lower().replace("_", "-"):
        raise ReleaseError(f"el componente raíz del SBOM es {found!r}, no {name!r}")
    if component.get("version") not in (None, "", version):
        raise ReleaseError(f"el componente raíz del SBOM ya tiene otra versión: {component['version']!r}")
    rebuilt: dict[str, object] = {}
    for key, value in component.items():
        if key in ("version", "purl"):
            continue
        rebuilt[key] = value
        if key == "name":
            rebuilt["version"] = version
            rebuilt["purl"] = f"pkg:pypi/{name.lower()}@{version}"
    metadata["component"] = rebuilt  # type: ignore[index]
    return doc


def changelog_section(text: str, version: str) -> str:
    """El cuerpo de ``## [<versión>]`` hasta la sección siguiente, sin líneas vacías en los bordes."""
    heads = list(_SECTION.finditer(text))
    for i, head in enumerate(heads):
        if head.group("name") != version:
            continue
        start = text.index("\n", head.end()) + 1 if "\n" in text[head.end():] else len(text)
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[start:end].strip("\n")
        if not body.strip():
            raise ReleaseError(f"la sección [{version}] de CHANGELOG.md está vacía")
        return body
    raise ReleaseError(f"CHANGELOG.md no tiene la sección [{version}]: escríbela antes de etiquetar")


def release_notes(changelog: str, *, name: str, version: str, repo: str, previous: str | None = None) -> str:
    """Las notas de la GitHub Release (01 §2.3)."""
    lines = [changelog_section(changelog, version), "", "---", "",
             "Verify the files of this release (10-seguridad REQ-SEC-52):", "", "```bash",
             f"pypi-attestations verify pypi --repository https://github.com/{repo} pypi:{name}-{version}-py3-none-any.whl",
             f"gh attestation verify {name}-{version}-py3-none-any.whl -R {repo}",
             "```"]
    if previous and not is_prerelease(version):
        owner = repo.split("/", 1)[0]
        lines += ["", f"Template changes: https://github.com/{owner}/{name}-template/compare/{previous}...v{version}"]
    return "\n".join(lines) + "\n"


def pypi_readme(text: str, *, repo: str, ref: str) -> str:
    """El README con sus rutas relativas como URL absolutas a ``ref`` en GitHub (la descripción de PyPI).

    Las imágenes van a ``raw.githubusercontent.com``, los archivos a ``blob/<ref>`` y las carpetas
    (``spec/``) a ``tree/<ref>``. Quedan igual las URL con esquema, las anclas (``#english``) y lo
    que esté dentro de un bloque de código. Aplicarla dos veces da lo mismo.
    """
    if not REPO_RE.fullmatch(repo) or not ref or any(c.isspace() for c in ref):
        raise ReleaseError(f"repo o ref inválidos: {repo!r}, {ref!r}")
    fences = [m.span() for m in _FENCE.finditer(text)]

    def absolute(path: str, *, image: bool) -> str:
        if path.startswith(("#", "/")) or _SCHEME.match(path):
            return path
        if image:
            return f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"
        file, sep, anchor = path.partition("#")
        kind = "tree" if file.endswith("/") else "blob"
        return f"https://github.com/{repo}/{kind}/{ref}/{file}{sep}{anchor}"

    def replace(m: re.Match[str]) -> str:
        if any(start <= m.start() < end for start, end in fences):
            return m.group(0)
        if m.group("attr"):
            return m.group("attr") + absolute(m.group("html"), image=m.group("attr").startswith("src"))
        return f'{m.group("bang")}[{m.group("label")}]({absolute(m.group("md"), image=bool(m.group("bang")))})'

    return _LINK.sub(replace, text)


def _cmd_check_tag(args: argparse.Namespace) -> None:
    version, prerelease = check_tag(args.tag, package_version())
    print(f"version={version}")
    print(f"prerelease={'true' if prerelease else 'false'}")


def _cmd_sbom_version(args: argparse.Namespace) -> None:
    path = Path(args.file)
    if not VERSION_RE.fullmatch(args.version):
        raise ReleaseError(f"versión inválida: {args.version!r}")
    doc = json.loads(path.read_text(encoding="utf-8"))
    set_sbom_version(doc, project_name(), args.version)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def _cmd_notes(args: argparse.Namespace) -> None:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    sys.stdout.write(release_notes(text, name=project_name(), version=args.version, repo=args.repo,
                                   previous=args.previous or None))


def _cmd_pypi_readme(args: argparse.Namespace) -> None:
    path = Path(args.file)
    text = pypi_readme(path.read_text(encoding="utf-8"), repo=args.repo, ref=args.ref)
    path.write_text(text, encoding="utf-8", newline="\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="release.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    tag = sub.add_parser("check-tag", help="la etiqueta es la versión del paquete")
    tag.add_argument("--tag", required=True)
    tag.set_defaults(func=_cmd_check_tag)
    sbom = sub.add_parser("sbom-version", help="versión del componente raíz del SBOM")
    sbom.add_argument("--file", required=True)
    sbom.add_argument("--version", required=True)
    sbom.set_defaults(func=_cmd_sbom_version)
    notes = sub.add_parser("notes", help="notas de la GitHub Release desde CHANGELOG.md")
    notes.add_argument("--version", required=True)
    notes.add_argument("--repo", required=True)
    notes.add_argument("--previous", default="")
    notes.set_defaults(func=_cmd_notes)
    readme = sub.add_parser("pypi-readme", help="el README de PyPI, con enlaces absolutos a la etiqueta")
    readme.add_argument("--repo", required=True)
    readme.add_argument("--ref", required=True)
    readme.add_argument("--file", default=str(ROOT / "README.md"))
    readme.set_defaults(func=_cmd_pypi_readme)
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (ReleaseError, OSError, ValueError, KeyError) as exc:
        print(f"release.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
