# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Comprobaciones globales de completitud que ninguna prueba de un módulo puede hacer sola.

Autoridad: 01-arquitectura REQ-ARCH-3, REQ-ARCH-21 y §9 (REQ-ARCH-69..74); 05-operaciones
REQ-OPS-6; 11-trazabilidad §7.2.

- Los módulos que el núcleo carga por nombre (``RULE_MODULES``, ``CI_MODULES`` y los de
  ``REGISTRY``) existen e importan.
- Por AST sobre ``src/``: cada clave literal que se pasa al traductor (``t()``, ``t.get()``,
  ``t.plural()`` y los ``help``/``description`` que son claves), cada código literal de
  ``make_finding``, ``OpError``, ``make_error`` y ``make_warning`` tiene texto en ``es`` y en
  ``en``, y cada marcador de ese texto llega en la llamada. Las claves que se arman con
  f-strings no se pueden ver así: para ``finding.<código>.*`` las cubre el registro de
  marcadores (``finding_params``) de todas las reglas y de ``config``.
- ``finding_params`` no tiene conflictos y no quedan textos ``finding.*`` huérfanos.
- La rueda lleva ``aucove/_data/{schemas,template,skills}``, ``gitleaks.toml``,
  ``required-paths.json``, ``tsa-catalog.json``, el kit del árbitro de ``evidence/``
  (REQ-ARCH-3) y los textos y la lista de licencias de ``init`` (11 §7.3; 00-marco D-64).
- ``template/base/LICENSES/`` es idéntico a ``LICENSES/`` de la raíz (11 §7.2).
"""

from __future__ import annotations

import ast
import importlib
import re
import subprocess
import sys
import zipfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest

from aucove import _meta
from aucove.domain.errors import ERRORS, WARNING_PARAMS, WARNINGS
from aucove.i18n import CATALOG_AREAS, LANGS, load_catalog, placeholders
from aucove.ops.registry import CI_MODULES, REGISTRY, CiCommand, load_op_module
from aucove.ops.rules.base import RULE_MODULES, UNAVAILABLE_CODE, RuleSpec
from tests.conftest import REPO_ROOT

SRC = REPO_ROOT / "src" / _meta.NAME
AREA_PREFIXES: tuple[str, ...] = tuple(sorted({p for prefixes in CATALOG_AREAS.values() for p in prefixes}))
TRANSLATOR_NAMES = frozenset({"t", "tr", "_t"})
# Parámetros con nombre de las funciones que no son marcadores de texto.
NOT_MARKERS: Mapping[str, frozenset[str]] = {
    "finding": frozenset({"severity", "path", "line", "pointer"}),
    "error": frozenset({"details", "data", "warnings"}),
    "warning": frozenset({"details"}),
}


@cache
def catalog(lang: str) -> Mapping[str, str]:
    return load_catalog(lang)  # type: ignore[arg-type]


# -- recolección por AST ------------------------------------------------------------------


@dataclass(frozen=True)
class Use:
    kind: str  # 'key' | 'plural' | 'finding' | 'error' | 'warning'
    key: str  # clave del catálogo, o código
    kwargs: frozenset[str] | None  # None si la llamada usa **kwargs
    where: str  # archivo:línea

    def __str__(self) -> str:
        return f"{self.where}: {self.key}"


def _callee_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_translator(node: ast.expr) -> bool:
    """``t``, ``x.t`` o ``x.t()``: expresiones que dan un ``Translator``."""
    if isinstance(node, ast.Name):
        return node.id in TRANSLATOR_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr == "t"
    if isinstance(node, ast.Call):
        return isinstance(node.func, ast.Attribute) and node.func.attr == "t" and not node.args
    return False


def _literal(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _kwargs(call: ast.Call, drop: frozenset[str] = frozenset()) -> frozenset[str] | None:
    if any(k.arg is None for k in call.keywords):
        return None
    return frozenset(str(k.arg) for k in call.keywords if k.arg not in drop)


def _looks_like_key(text: str) -> bool:
    return text.startswith(AREA_PREFIXES) and "." in text and " " not in text


def _uses_in(path: Path) -> Iterator[Use]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rel = path.relative_to(REPO_ROOT).as_posix()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        where = f"{rel}:{node.lineno}"
        func = node.func
        first = _literal(node.args[0]) if node.args else None
        # Traductor: t("k"), x.t("k"), x.t()("k"), t.get("k"), t.plural("k", n).
        if first is not None and _looks_like_key(first):
            if _is_translator(func):
                yield Use("key", first, _kwargs(node), where)
            elif isinstance(func, ast.Attribute) and func.attr in ("get", "plural") and _is_translator(func.value):
                yield Use("plural" if func.attr == "plural" else "key", first, _kwargs(node), where)
        # help= y description= de argparse que son claves (se traducen al armar el parser).
        for kw in node.keywords:
            text = _literal(kw.value)
            if kw.arg in ("help", "description") and text is not None and _looks_like_key(text):
                yield Use("key", text, frozenset(), where)
        name = _callee_name(func)
        if name == "make_finding":
            code = _literal(node.args[2]) if len(node.args) >= 3 else next(
                (_literal(k.value) for k in node.keywords if k.arg == "code"), None
            )
            if code is not None:
                yield Use("finding", code, _kwargs(node, NOT_MARKERS["finding"] | {"code"}), where)
        elif name in ("OpError", "make_error") and first is not None:
            yield Use("error", first, _kwargs(node, NOT_MARKERS["error"]), where)
        elif name == "make_warning" and first is not None:
            yield Use("warning", first, _kwargs(node, NOT_MARKERS["warning"]), where)


@cache
def uses() -> tuple[Use, ...]:
    out: list[Use] = []
    for path in sorted(SRC.rglob("*.py")):
        out.extend(_uses_in(path))
    return tuple(out)


def _of(kind: str) -> list[Use]:
    return [u for u in uses() if u.kind == kind]


def _texts(key: str) -> dict[str, str | None]:
    return {lang: catalog(lang).get(key) for lang in LANGS}


# -- módulos cargados por nombre -------------------------------------------------------


@pytest.mark.parametrize("module", RULE_MODULES)
def test_rule_modules_import_and_export_rules(module: str) -> None:
    mod = importlib.import_module(f".{module}", _meta.NAME)
    rules = getattr(mod, "RULES", None)
    assert isinstance(rules, tuple) and rules, f"{module}: RULES vacío o ausente"
    assert all(isinstance(r, RuleSpec) for r in rules)


@pytest.mark.parametrize("module", CI_MODULES)
def test_ci_modules_import_and_export_commands(module: str) -> None:
    mod = importlib.import_module(f".{module}", _meta.NAME)
    commands = getattr(mod, "COMMANDS", None)
    assert isinstance(commands, tuple) and commands, f"{module}: COMMANDS vacío o ausente"
    assert all(isinstance(c, CiCommand) and callable(c.main) and callable(c.add_arguments) for c in commands)


def test_ci_command_names_are_unique() -> None:
    names = [c.name for m in CI_MODULES for c in importlib.import_module(f".{m}", _meta.NAME).COMMANDS]
    assert len(names) == len(set(names)), names


def test_the_rule_catalog_loads_every_module() -> None:
    from aucove.ops.rules.catalog import load_rules

    rules = load_rules()
    found = {r.run.__module__.removeprefix(f"{_meta.NAME}.") for r in rules}
    assert found == set(RULE_MODULES)


@pytest.mark.parametrize("spec", REGISTRY, ids=[s.id for s in REGISTRY])
def test_registry_modules_exist(spec: object) -> None:
    mod = load_op_module(spec)  # type: ignore[arg-type]
    assert callable(getattr(mod, "run", None)), f"{mod.__name__} no exporta run"
    render_text = getattr(mod, "render_text", None)
    assert render_text is None or callable(render_text)


# -- claves y marcadores por AST --------------------------------------------------------


def test_the_scanner_sees_the_calls() -> None:
    """Si la recolección se rompe, las pruebas de abajo pasarían sin mirar nada."""
    counts = {kind: len(_of(kind)) for kind in ("key", "plural", "finding", "error", "warning")}
    assert counts["key"] >= 200 and counts["error"] >= 30 and counts["finding"] >= 5, counts
    assert counts["plural"] >= 1 and counts["warning"] >= 3, counts


def test_literal_translator_keys_exist_in_es_and_en() -> None:
    missing: list[str] = []
    for use in _of("key"):
        missing.extend(f"{use} ({lang})" for lang, text in _texts(use.key).items() if text is None)
    for use in _of("plural"):
        for form in ("one", "other"):
            missing.extend(
                f"{use}.{form} ({lang})" for lang, text in _texts(f"{use.key}.{form}").items() if text is None
            )
    assert missing == []


def test_literal_translator_keys_get_their_placeholders() -> None:
    problems: list[str] = []
    for use in _of("key") + _of("plural"):
        if use.kwargs is None:
            continue  # **kwargs: no se puede saber estáticamente
        given = use.kwargs | ({"n"} if use.kind == "plural" else set())
        keys = [f"{use.key}.one", f"{use.key}.other"] if use.kind == "plural" else [use.key]
        for key in keys:
            for lang, text in _texts(key).items():
                if text is not None and not placeholders(text) <= given:
                    problems.append(f"{use.where}: {key} ({lang}) pide {sorted(placeholders(text) - given)}")
    assert problems == []


# Cadenas con forma de clave que no son claves del catálogo: rutas de project.yaml y los ids de
# operación (05 §3.2), que comparten prefijo con un área (review.list y el área review).
NOT_KEYS: frozenset[str] = frozenset({"ci.model_review", "forge.repo"}) | {spec.id for spec in REGISTRY}
_KEY_RE = re.compile(r"^[a-z_]+(?:\.[A-Za-z0-9_\-]+)+$")
_FILE_SUFFIXES = (".yaml", ".yml", ".toml", ".json", ".md", ".tex", ".bib", ".txt")


def test_every_key_like_constant_exists_in_es_and_en() -> None:
    """También las claves que llegan al traductor por un ``if`` o por un ayudante (``say``).

    Una cadena con forma ``<área>.<…>`` es una clave (o la base de un plural ``.one`` y
    ``.other``) salvo que sea un nombre de archivo, un prefijo para concatenar (termina en
    ``_``) o una ruta de ``project.yaml`` o un id de operación de ``NOT_KEYS``.
    """
    problems: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            text = node.value
            if not (text.startswith(AREA_PREFIXES) and _KEY_RE.match(text)):
                continue
            if text in NOT_KEYS or text.endswith(_FILE_SUFFIXES) or text.endswith("_"):
                continue
            for lang in LANGS:
                cat = catalog(lang)
                if text not in cat and not (f"{text}.one" in cat and f"{text}.other" in cat):
                    problems.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}: {text} ({lang})")
    assert problems == []


def test_literal_finding_codes_have_texts_with_the_given_placeholders() -> None:
    problems: list[str] = []
    for use in _of("finding"):
        for lang, text in _texts(f"finding.{use.key}.message").items():
            if text is None:
                problems.append(f"{use} sin finding.{use.key}.message ({lang})")
                continue
            hint = catalog(lang).get(f"finding.{use.key}.hint") or ""
            needed = placeholders(text) | placeholders(hint)
            if use.kwargs is not None and not needed <= use.kwargs:
                problems.append(f"{use} ({lang}) pide {sorted(needed - use.kwargs)}")
    assert problems == []


def test_literal_error_codes_are_in_the_catalog() -> None:
    problems: list[str] = []
    for use in _of("error"):
        spec = ERRORS.get(use.key)
        if spec is None:
            problems.append(f"{use}: no está en ERRORS")
            continue
        if use.kwargs is not None and use.kwargs != spec.params:
            problems.append(f"{use}: marcadores {sorted(use.kwargs)} != {sorted(spec.params)}")
        for part in ("message", "hint"):
            for lang, text in _texts(f"error.{use.key}.{part}").items():
                if text is None:
                    problems.append(f"{use}: falta error.{use.key}.{part} ({lang})")
                elif not placeholders(text) <= spec.params:
                    problems.append(f"{use}: error.{use.key}.{part} ({lang}) con marcadores fuera de params")
    assert problems == []


def test_literal_warning_codes_are_in_the_catalog() -> None:
    problems: list[str] = []
    for use in _of("warning"):
        if use.key not in WARNINGS:
            problems.append(f"{use}: no está en WARNINGS")
            continue
        params = WARNING_PARAMS.get(use.key, frozenset())
        if use.kwargs is not None and use.kwargs != params:
            problems.append(f"{use}: marcadores {sorted(use.kwargs)} != {sorted(params)}")
        for lang, text in _texts(f"warning.{use.key}").items():
            if text is None:
                problems.append(f"{use}: falta warning.{use.key} ({lang})")
            elif not placeholders(text) <= params:
                problems.append(f"{use}: warning.{use.key} ({lang}) con marcadores fuera de params")
    assert problems == []


# -- registro de marcadores de hallazgo -------------------------------------------


@cache
def all_finding_params() -> Mapping[str, frozenset[str]]:
    from aucove.ops.rules.catalog import finding_params, load_rules

    return finding_params(load_rules())


def test_finding_params_has_no_conflicts() -> None:
    params = all_finding_params()  # ValueError si dos fuentes declaran marcadores distintos
    from aucove.ops.rules.catalog import load_rules

    for rule in load_rules():
        for code, markers in rule.codes.items():
            assert params[code] == markers, (rule.id, code)
    assert UNAVAILABLE_CODE in params


def test_every_declared_finding_code_has_texts_with_its_placeholders() -> None:
    problems: list[str] = []
    for code, markers in sorted(all_finding_params().items()):
        for lang in LANGS:
            message = catalog(lang).get(f"finding.{code}.message")
            if message is None:
                problems.append(f"finding.{code}.message ({lang})")
                continue
            hint = catalog(lang).get(f"finding.{code}.hint") or ""
            extra = (placeholders(message) | placeholders(hint)) - markers
            if extra:
                problems.append(f"finding.{code} ({lang}) usa {sorted(extra)}, que no se declaran")
    assert problems == []


def test_no_orphan_finding_texts() -> None:
    """Cada ``finding.<código>.*`` de los catálogos es de un código que alguien emite."""
    declared = set(all_finding_params())
    for lang in LANGS:
        # Los códigos pueden llevar puntos (conv.tree.missing): finding.<código>.message|hint.
        codes = {key.removeprefix("finding.").rsplit(".", 1)[0] for key in catalog(lang) if key.startswith("finding.")}
        assert codes - declared == set(), f"{lang}: textos de hallazgo sin código declarado"


def test_finding_and_message_keys_have_the_same_placeholders_in_es_and_en() -> None:
    problems: list[str] = []
    es, en = catalog("es"), catalog("en")
    for key in sorted(set(es) & set(en)):
        if placeholders(es[key]) != placeholders(en[key]):
            problems.append(key)
    assert problems == []


# -- rueda (REQ-ARCH-3) ------------------------------------------------------------


def _files_under(root: Path) -> set[str]:
    return {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    }


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> zipfile.ZipFile:
    pytest.importorskip("hatchling")
    out = tmp_path_factory.mktemp("wheel")
    proc = subprocess.run(
        [sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-d", str(out)],
        cwd=REPO_ROOT, capture_output=True, timeout=600, check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    wheels = list(out.glob("*.whl"))
    assert len(wheels) == 1, wheels
    return zipfile.ZipFile(wheels[0])


def test_wheel_contains_the_packaged_data(wheel: zipfile.ZipFile) -> None:
    names = set(wheel.namelist())
    data = f"{_meta.NAME}/_data"
    for kind in ("schemas", "template", "skills"):
        source = REPO_ROOT / kind
        expected = {f"{data}/{kind}/{rel}" for rel in _files_under(source)}
        assert expected, f"{kind}/ está vacío"
        missing = sorted(expected - names)
        assert missing == [], f"faltan en la rueda: {missing[:10]}"
        for rel in sorted(_files_under(source)):
            assert wheel.read(f"{data}/{kind}/{rel}") == (source / rel).read_bytes(), f"{kind}/{rel}"
    kit = ("evidence/VERIFY.es.md", "evidence/VERIFY.en.md", "evidence/open_commitments.py")
    licenses = ("spdx-licenses.json", *(f"LICENSES/{p.name}" for p in sorted((SRC / "_data" / "LICENSES").iterdir())))
    assert len(licenses) == 10  # la lista de SPDX y los 9 textos de 11-trazabilidad §7.3 (00-marco D-64)
    for name in ("gitleaks.toml", "required-paths.json", "tsa-catalog.json", *kit, *licenses):
        member = f"{data}/{name}"
        assert member in names, member
        assert wheel.read(member) == (SRC / "_data" / name).read_bytes()


def test_wheel_contains_the_catalogs_and_the_entry_point(wheel: zipfile.ZipFile) -> None:
    names = set(wheel.namelist())
    catalogs = SRC / "i18n" / "catalogs"
    for rel in _files_under(catalogs):
        assert f"{_meta.NAME}/i18n/catalogs/{rel}" in names, rel
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
    entry_points = next(n for n in names if n.endswith(".dist-info/entry_points.txt"))
    text = wheel.read(entry_points).decode("utf-8")
    assert f"{_meta.NAME} = {_meta.NAME}.entry:main" in text


# -- licencias de la plantilla (11-trazabilidad §7.2) -----------------------------------


def test_template_licenses_are_identical_to_the_framework_ones() -> None:
    template = REPO_ROOT / "template" / "base" / "LICENSES"
    root = REPO_ROOT / "LICENSES"
    texts = sorted(p.name for p in template.iterdir() if p.is_file())
    assert texts, "template/base/LICENSES/ está vacío"
    missing = [name for name in texts if not (root / name).is_file()]
    assert missing == [], (
        f"LICENSES/ de la raíz no tiene {missing}: hay que bajar los textos oficiales de "
        "spdx/license-list-data en binario (CONTRIBUTING.md) y copiarlos a template/base/LICENSES/"
    )
    different = [name for name in texts if (template / name).read_bytes() != (root / name).read_bytes()]
    assert different == []

