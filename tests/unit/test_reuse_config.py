# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""REUSE.toml, LICENSES/ y archivos de gobierno del repositorio del framework.

Cubre 11-trazabilidad §7.1, §7.5 y §7.6, 01-arquitectura §2.2 y REQ-ARCH-4, y
REQ-TRACE-56 y REQ-TRACE-58. `reuse lint` (herramienta externa, REQ-TRACE-59) sigue siendo la
comprobación de REUSE 3.3; estas pruebas fijan lo que el lint no ve: el contenido
exacto de REUSE.toml, que cada licencia anotada tenga su texto y que no sobre
ninguno, y los requisitos de CONTRIBUTING.md, AGENTS.md y .claude/settings.json.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LICENSES_DIR = REPO_ROOT / "LICENSES"
HOLDER = "2026 The aucove Authors"
SPDX_HOLDER = "Linux Foundation and its Contributors"  # spdx/license-list-data

# Las claves y etiquetas SPDX se arman por concatenación para que reuse no las
# lea como información de licencia de este archivo.
_SPDX = "SPDX" + "-"
KEY_COPYRIGHT = _SPDX + "FileCopyrightText"
KEY_LICENSE = _SPDX + "License-Identifier"
# Tampoco se escribe la palabra seguida del titular: reuse la tomaría por un
# aviso de copyright de este archivo.
COPYRIGHT_LINE = "Copy" + f"right {HOLDER}"

# REUSE.toml de la raíz, exactamente el esperado (gana la última tabla que coincide).
EXPECTED_REUSE_TOML: dict[str, object] = {
    "version": 1,
    "annotations": [
        {"path": ["**"], KEY_COPYRIGHT: HOLDER, KEY_LICENSE: "Apache-2.0"},
        {"path": ["schemas/**"], KEY_COPYRIGHT: HOLDER, KEY_LICENSE: "Apache-2.0"},
        {"path": ["spec/**"], KEY_COPYRIGHT: HOLDER, KEY_LICENSE: "CC-BY-4.0"},
        {"path": ["docs/**"], KEY_COPYRIGHT: HOLDER, KEY_LICENSE: "CC-BY-4.0"},
        {"path": ["skills/**"], KEY_COPYRIGHT: HOLDER, KEY_LICENSE: "MIT-0"},
        {"path": ["template/l10n/**", "template/formats/*/l10n/**"], KEY_COPYRIGHT: HOLDER, KEY_LICENSE: "CC0-1.0"},
        {
            "path": [
                "template/l10n/*/tex/preamble.tex",
                "template/l10n/*/.github/**",
                "template/l10n/*/AGENTS.md",
                "template/l10n/*/CLAUDE.md",
                "template/formats/*/l10n/*/tex/preamble.tex",
                "template/formats/*/l10n/*/.github/**",
                "template/formats/*/l10n/*/AGENTS.md",
                "template/formats/*/l10n/*/CLAUDE.md",
            ],
            KEY_COPYRIGHT: HOLDER,
            KEY_LICENSE: "MIT-0",
        },
        # La lista de licencias de SPDX que usa init (11 REQ-TRACE-62): datos de terceros, CC0.
        {"path": ["src/aucove/_data/spdx-licenses.json"], KEY_COPYRIGHT: SPDX_HOLDER, KEY_LICENSE: "CC0-1.0"},
    ],
}

# Único REUSE.toml anidado permitido: el de la plantilla (11 §7.2).
ALLOWED_NESTED_REUSE_TOML: frozenset[str] = frozenset({"template/base/REUSE.toml"})

# Directorios que no forman parte del árbol versionado.
WALK_SKIP: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        ".venv-tools",
        ".aucove",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".hypothesis",
        ".tox",
        "node_modules",
        "dist",
        "build",
    }
)
HEADER_DIRS: tuple[str, ...] = ("src", "tools", "tests")

# Hash del texto canónico de la ASF (https://www.apache.org/licenses/LICENSE-2.0.txt).
ASF_APACHE_2_0_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"

# Marca reconocible del comienzo de cada texto de LICENSES/. Es una comprobación de
# cordura, porque sin red no se puede comparar con la fuente oficial. Además,
# Apache-2.0.txt se compara byte a byte con LICENSE (hash de la ASF), y CC0-1.0.txt
# y MIT-0.txt con template/base/LICENSES/ (CONTRIBUTING.md).
LICENSE_TITLES: dict[str, str] = {
    "Apache-2.0": "Apache License",
    "MIT-0": "MIT No Attribution",
    "CC0-1.0": "CC0 1.0 Universal",
}

# Aviso de no afiliación de 11-trazabilidad §7.6 (REQ-TRACE-71), sin el énfasis de Markdown.
NOTICE_ES = (
    "aucove es un proyecto independiente de código abierto. No está afiliado a Anthropic, "
    "OpenAI, Google, GitHub ni a otros proveedores mencionados, ni cuenta con su respaldo o "
    "patrocinio. Claude, Claude Code, Codex, Gemini, GitHub y las demás marcas pertenecen a sus "
    "titulares y se mencionan solo para indicar compatibilidad."
)
NOTICE_EN = (
    "aucove is an independent open source project. It is not affiliated with, endorsed by, or "
    "sponsored by Anthropic, OpenAI, Google, GitHub, or any other provider mentioned. Claude, "
    "Claude Code, Codex, Gemini, GitHub, and other marks belong to their respective owners and "
    "are mentioned only to indicate compatibility."
)

# Archivos de texto de gobierno que deben ir en UTF-8 sin BOM, con LF y salto final.
GOVERNANCE_TEXT_FILES: tuple[str, ...] = (
    "README.md",
    "LICENSE",
    "NOTICE",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
    "REUSE.toml",
    "AGENTS.md",
    "CLAUDE.md",
    ".claude/settings.json",
    "deploy/README.md",
    "skills/LICENSE",
)

_LICENSE_ID = re.compile(r"^(LicenseRef-[A-Za-z0-9.-]+|[A-Za-z0-9][A-Za-z0-9.+-]*)$")
_OPERATORS = frozenset({"AND", "OR", "WITH"})


def _read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _normalize(text: str) -> str:
    """Quita el énfasis y las citas de Markdown y colapsa los espacios."""
    text = re.sub(r"^\s*>\s?", "", text, flags=re.MULTILINE)
    return " ".join(text.replace("*", "").split())


def license_ids(expression: str) -> set[str]:
    """Identificadores de una expresión SPDX, sin operadores ni excepciones (WITH x)."""
    tokens = [t for t in re.split(r"[()\s]+", expression) if t]
    ids: set[str] = set()
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token == "WITH":
            skip_next = True
            continue
        if token in _OPERATORS:
            continue
        ids.add(token.removesuffix("+"))
    return ids


def _walk_repo(root: Path) -> Iterator[tuple[Path, list[str], list[str]]]:
    for current, subdirs, files in os.walk(root):
        subdirs[:] = sorted(d for d in subdirs if d not in WALK_SKIP)
        yield Path(current), subdirs, sorted(files)


def nested_reuse_tomls(root: Path) -> list[str]:
    """REUSE.toml bajo ``root`` distintos del de la raíz, como rutas relativas con '/'."""
    found: list[str] = []
    for current, _subdirs, files in _walk_repo(root):
        if current == root:
            continue
        if "REUSE.toml" in files:
            found.append((current / "REUSE.toml").relative_to(root).as_posix())
    return found


def _toml_licenses(path: Path) -> set[str]:
    data = tomllib.loads(_read_text(path))
    ids: set[str] = set()
    for annotation in data.get("annotations", []):
        expression = annotation.get(KEY_LICENSE)
        expressions = expression if isinstance(expression, list) else [expression]
        for item in expressions:
            if isinstance(item, str):
                ids |= license_ids(item)
    return ids


def _header_licenses(root: Path) -> set[str]:
    """Licencias de las cabeceras de los .py de src/, tools/ y tests/ (primeras líneas)."""
    prefix = f"# {KEY_LICENSE}:"
    ids: set[str] = set()
    for name in HEADER_DIRS:
        base = root / name
        if not base.is_dir():
            continue
        for current, _subdirs, files in _walk_repo(base):
            for file_name in files:
                if not file_name.endswith(".py"):
                    continue
                try:
                    lines = _read_text(current / file_name).split("\n")[:3]
                except UnicodeDecodeError:
                    continue  # lo reporta test_spdx_headers
                for line in lines:
                    if line.startswith(prefix):
                        ids |= license_ids(line[len(prefix) :].strip())
    return ids


def annotated_licenses(root: Path) -> dict[str, list[str]]:
    """Licencia -> fuentes que la anotan (REUSE.toml de la raíz, anidados y cabeceras)."""
    sources: dict[str, list[str]] = {}
    tomls = [root / "REUSE.toml"] + [root / rel for rel in nested_reuse_tomls(root)]
    for path in tomls:
        if path.is_file():
            for license_id in _toml_licenses(path):
                sources.setdefault(license_id, []).append(path.relative_to(root).as_posix())
    for license_id in _header_licenses(root):
        sources.setdefault(license_id, []).append("cabeceras .py")
    return sources


def license_texts(root: Path) -> dict[str, Path]:
    directory = root / "LICENSES"
    if not directory.is_dir():
        return {}
    return {p.stem: p for p in sorted(directory.iterdir()) if p.is_file() and p.suffix == ".txt"}


# --- REUSE.toml --------------------------------------------------------------


def test_reuse_toml_is_exactly_c27() -> None:
    data = tomllib.loads(_read_text(REPO_ROOT / "REUSE.toml"))
    assert data == EXPECTED_REUSE_TOML


def test_reuse_toml_uses_the_project_holder() -> None:
    # D-07 (b): el titular es «The aucove Authors» mientras nadie confirme otro (REQ-TRACE-73).
    # La única excepción son los datos de terceros: la lista de licencias de SPDX.
    data = tomllib.loads(_read_text(REPO_ROOT / "REUSE.toml"))
    own = {annotation[KEY_COPYRIGHT] for annotation in data["annotations"] if annotation[KEY_COPYRIGHT] != SPDX_HOLDER}
    assert own == {HOLDER}
    third = [a["path"] for a in data["annotations"] if a[KEY_COPYRIGHT] == SPDX_HOLDER]
    assert third == [["src/aucove/_data/spdx-licenses.json"]]


def test_only_the_template_has_a_nested_reuse_toml() -> None:
    unexpected = sorted(set(nested_reuse_tomls(REPO_ROOT)) - ALLOWED_NESTED_REUSE_TOML)
    assert unexpected == [], f"REUSE.toml anidados no previstos: {unexpected}"


# --- LICENSES/ ---------------------------------------------------------------


def test_every_annotated_license_has_its_text() -> None:
    annotated = annotated_licenses(REPO_ROOT)
    texts = license_texts(REPO_ROOT)
    missing = {lid: srcs for lid, srcs in sorted(annotated.items()) if lid not in texts}
    assert missing == {}, (
        "Licencias anotadas sin texto en LICENSES/ (copia en binario text/<id>.txt de "
        "spdx/license-list-data, por ejemplo con curl -fsSL -o LICENSES/<id>.txt; en Windows, "
        "'reuse download' escribe CRLF): "
        + ", ".join(f"{lid} (en {', '.join(srcs)})" for lid, srcs in missing.items())
    )


def test_no_unused_license_texts() -> None:
    # REUSE prohíbe los textos sin uso: CC-BY-4.0 está porque la usan spec/ (00-marco D-51) y docs/.
    annotated = annotated_licenses(REPO_ROOT)
    unused = sorted(set(license_texts(REPO_ROOT)) - set(annotated))
    assert unused == [], f"Textos de LICENSES/ que ninguna anotación usa: {unused}"


def test_licenses_dir_holds_only_license_texts() -> None:
    if not LICENSES_DIR.is_dir():
        pytest.skip("LICENSES/ todavía no existe (lo reporta test_every_annotated_license_has_its_text)")
    bad = [
        p.name
        for p in sorted(LICENSES_DIR.iterdir())
        if not (p.is_file() and p.suffix == ".txt" and _LICENSE_ID.match(p.stem))
    ]
    assert bad == [], f"Entradas de LICENSES/ que no son <id>.txt: {bad}"


def test_license_texts_are_plain_utf8() -> None:
    problems: list[str] = []
    for license_id, path in license_texts(REPO_ROOT).items():
        data = path.read_bytes()
        if data.startswith(b"\xef\xbb\xbf"):
            problems.append(f"{path.name}: BOM")
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{path.name}: no es UTF-8")
            continue
        if "\r" in text:
            problems.append(f"{path.name}: CR o CRLF")
        if not text.strip():
            problems.append(f"{path.name}: vacío")
        title = LICENSE_TITLES.get(license_id)
        if title is not None and title not in text[:400]:
            problems.append(f"{path.name}: no empieza como el texto de {license_id}")
    assert problems == [], "; ".join(problems)


def test_root_license_is_the_asf_apache_2_0_text() -> None:
    digest = hashlib.sha256((REPO_ROOT / "LICENSE").read_bytes()).hexdigest()
    assert digest == ASF_APACHE_2_0_SHA256


def test_apache_license_text_is_the_root_license() -> None:
    # CONTRIBUTING.md: LICENSES/Apache-2.0.txt es el mismo texto canónico de la ASF que LICENSE.
    reference = LICENSES_DIR / "Apache-2.0.txt"
    assert reference.is_file(), "falta LICENSES/Apache-2.0.txt"
    assert reference.read_bytes() == (REPO_ROOT / "LICENSE").read_bytes()


def test_skills_license_is_mit_0() -> None:
    # 01-arquitectura §2.1 y REQ-TRACE-57: skills/ va en MIT-0.
    text = _read_text(REPO_ROOT / "skills" / "LICENSE")
    lines = text.split("\n")
    assert lines[0] == "MIT No Attribution"
    assert COPYRIGHT_LINE in lines
    reference = LICENSES_DIR / "MIT-0.txt"
    if not reference.is_file():
        pytest.skip("LICENSES/MIT-0.txt todavía no existe; no se puede comparar el cuerpo")
    marker = "Permission is hereby granted"
    spdx_text = _read_text(reference)
    assert marker in text and marker in spdx_text
    body = _normalize(text[text.index(marker) :])
    spdx_body = _normalize(spdx_text[spdx_text.index(marker) :])
    assert body == spdx_body


def test_template_license_texts_match_the_framework_copies() -> None:
    template_dir = REPO_ROOT / "template" / "base" / "LICENSES"
    if not template_dir.is_dir():
        pytest.skip("template/base/LICENSES/ no existe todavía")
    compared = 0
    for path in sorted(template_dir.glob("*.txt")):
        reference = LICENSES_DIR / path.name
        if not reference.is_file():
            continue
        assert path.read_bytes() == reference.read_bytes(), path.name
        compared += 1
    if compared == 0:
        pytest.skip("no hay textos comunes que comparar entre LICENSES/ y template/base/LICENSES/")


# --- Licencias en los fixtures y REQ-TRACE-59 --------------------------------


def test_fixtures_carry_no_licensing_artifacts() -> None:
    fixtures = REPO_ROOT / "tests" / "fixtures"
    if not fixtures.is_dir():
        pytest.skip("tests/fixtures/ no existe todavía")
    problems: list[str] = []
    for current, subdirs, files in _walk_repo(fixtures):
        rel_dir = current.relative_to(REPO_ROOT).as_posix()
        if "LICENSES" in subdirs:
            problems.append(f"{rel_dir}/LICENSES/")
        for name in files:
            rel = f"{rel_dir}/{name}"
            if name == "REUSE.toml" or name.endswith(".license"):
                problems.append(rel)
            elif not name.endswith(".py"):
                # Los .py llevan su cabecera; el resto no puede traer etiquetas SPDX.
                if _SPDX.encode("ascii") in (current / name).read_bytes():
                    problems.append(f"{rel} (etiqueta SPDX literal)")
    assert problems == [], "Artefactos de licencia en los fixtures: " + ", ".join(problems)


def test_core_does_not_import_reuse() -> None:
    # REQ-TRACE-59: reuse incluye código GPL-3.0-or-later; solo se invoca como herramienta.
    src = REPO_ROOT / "src"
    offenders: list[str] = []
    for current, _subdirs, files in _walk_repo(src) if src.is_dir() else ():
        for name in files:
            if not name.endswith(".py"):
                continue
            path = current / name
            tree = ast.parse(path.read_bytes(), filename=str(path))
            for node in ast.walk(tree):
                modules: list[str] = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    modules = [node.module]
                if any(m == "reuse" or m.startswith("reuse.") for m in modules):
                    offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}")
    assert offenders == [], f"El núcleo importa reuse: {offenders}"


# --- Gobierno del repositorio -------------------------------------------------


def test_claude_settings_only_fixes_commit_attribution() -> None:
    # REQ-TRACE-68: el framework fija attribution.commit = "" y nada más.
    data = json.loads(_read_text(REPO_ROOT / ".claude" / "settings.json"))
    assert data == {"attribution": {"commit": ""}}


def test_agents_md_asks_for_assisted_by() -> None:
    agents = _read_text(REPO_ROOT / "AGENTS.md")
    assert "Assisted-by: <tool>:<model>" in agents
    assert "No agregues `Signed-off-by`" in agents
    assert "Never add `Signed-off-by`" in agents
    assert "Co-authored-by" in agents
    claude = _read_text(REPO_ROOT / "CLAUDE.md")
    assert "\n@AGENTS.md\n" in claude


@pytest.mark.parametrize("name", ["README.md", "CONTRIBUTING.md"])
def test_non_affiliation_notice(name: str) -> None:
    # REQ-TRACE-71: aviso de no afiliación en es y en.
    text = _normalize(_read_text(REPO_ROOT / name))
    assert NOTICE_ES in text
    assert NOTICE_EN in text


def test_contributing_documents_dco_and_ai_policy() -> None:
    text = _read_text(REPO_ROOT / "CONTRIBUTING.md")
    required = [
        # DCO con la exención de Dependabot (REQ-TRACE-66, D-07 c).
        "https://developercertificate.org/",
        "`git commit -s`",
        "`dependabot[bot]`",
        "require contributors to sign off on web-based commits",
        # Política de IA (REQ-TRACE-67), en español y en inglés.
        "**Los agentes de IA NO DEBEN agregar `Signed-off-by`.**",
        "**AI agents MUST NOT add `Signed-off-by`.**",
        "**La persona revisa y prueba todo lo generado y responde por ello**",
        "**The person reviews and tests everything that was generated and is responsible for it**",
        "**Los commits asistidos llevan `Assisted-by: <tool>:<model>`**",
        "**Assisted commits carry `Assisted-by: <tool>:<model>`**",
        "**La IA no figura como `Co-authored-by`.**",
        "**AI is not listed as `Co-authored-by`.**",
        'assisted-tr   = %s"Assisted-by: " slug ":" model LF',
        # Entorno local.
        "### Entorno local",
        "### Local environment",
        "uv sync --locked --group dev",
        "reuse==6.2.0",
    ]
    missing = [item for item in required if item not in text]
    assert missing == [], f"Falta en CONTRIBUTING.md: {missing}"


def test_notice_keeps_the_ownership_note_and_the_evidence_extra() -> None:
    text = _read_text(REPO_ROOT / "NOTICE")
    assert text.split("\n")[1] == COPYRIGHT_LINE
    assert "D-07 b" in text and "REQ-TRACE-73" in text
    # D-07 (d): NOTICE lista las dependencias del extra evidence, que no van en la rueda.
    for package in ("opentimestamps", "python-bitcoinlib", "rfc3161-client", "cryptography"):
        assert package in text, package


def test_deploy_is_only_a_reserved_readme() -> None:
    # REQ-ARCH-4: deploy/ no contiene código en v0.1, solo su README.md.
    deploy = REPO_ROOT / "deploy"
    entries = sorted(p.relative_to(deploy).as_posix() for p in deploy.rglob("*"))
    assert entries == ["README.md"]
    assert "REQ-ARCH-4" in _read_text(deploy / "README.md")


@pytest.mark.parametrize("rel", GOVERNANCE_TEXT_FILES)
def test_text_files_are_utf8_lf(rel: str) -> None:
    data = (REPO_ROOT / rel).read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), "BOM"
    data.decode("utf-8")
    assert b"\r" not in data, "CR o CRLF"
    assert data.endswith(b"\n") and not data.endswith(b"\n\n"), "sin un único salto final"


# --- Pruebas de las funciones auxiliares ---------------------------------------


def test_license_ids_parses_spdx_expressions() -> None:
    assert license_ids("Apache-2.0") == {"Apache-2.0"}
    assert license_ids("Apache-2.0 AND MIT-0 AND CC0-1.0") == {"Apache-2.0", "MIT-0", "CC0-1.0"}
    assert license_ids("(MIT OR Apache-2.0) AND GPL-2.0-or-later WITH Classpath-exception-2.0") == {
        "MIT",
        "Apache-2.0",
        "GPL-2.0-or-later",
    }
    assert license_ids("LicenseRef-AllRightsReserved") == {"LicenseRef-AllRightsReserved"}


def test_annotated_licenses_reads_root_nested_and_headers(tmp_path: Path) -> None:
    def annotation(path: str, license_id: str) -> str:
        return (
            "[[annotations]]\n"
            f"path = ['{path}']\n"
            f"{KEY_COPYRIGHT} = '{HOLDER}'\n"
            f"{KEY_LICENSE} = '{license_id}'\n"
        )

    (tmp_path / "REUSE.toml").write_text("version = 1\n" + annotation("**", "Apache-2.0"), "utf-8")
    nested = tmp_path / "template" / "base"
    nested.mkdir(parents=True)
    (nested / "REUSE.toml").write_text("version = 1\n" + annotation("**", "CC0-1.0 OR MIT-0"), "utf-8")
    tools = tmp_path / "tools"
    tools.mkdir()
    header = f"# {KEY_COPYRIGHT}: {HOLDER}\n# {KEY_LICENSE}: BSD-3-Clause\n"
    (tools / "x.py").write_text(header, "utf-8")
    ignored = tmp_path / ".venv" / "lib"
    ignored.mkdir(parents=True)
    (ignored / "REUSE.toml").write_text("version = 1\n" + annotation("**", "GPL-3.0-only"), "utf-8")

    assert nested_reuse_tomls(tmp_path) == ["template/base/REUSE.toml"]
    assert annotated_licenses(tmp_path) == {
        "Apache-2.0": ["REUSE.toml"],
        "CC0-1.0": ["template/base/REUSE.toml"],
        "MIT-0": ["template/base/REUSE.toml"],
        "BSD-3-Clause": ["cabeceras .py"],
    }

    licenses = tmp_path / "LICENSES"
    licenses.mkdir()
    for name in ("Apache-2.0.txt", "CC-BY-4.0.txt"):
        (licenses / name).write_text("texto\n", "utf-8")
    assert sorted(license_texts(tmp_path)) == ["Apache-2.0", "CC-BY-4.0"]
    assert sorted(set(license_texts(tmp_path)) - set(annotated_licenses(tmp_path))) == ["CC-BY-4.0"]
