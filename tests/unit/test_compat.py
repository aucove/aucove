# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""domain/compat.py: 01-arquitectura §6.3, REQ-SCHEMA-17 y el subconjunto de PEP 440 de ``core``.

La implementación es solo stdlib (``domain``); aquí se compara con ``packaging``
(dependencia del núcleo, REQ-SCHEMA-17), con ``prereleases=True`` porque la
versión que se evalúa es la instalada (PEP 440).
"""

from __future__ import annotations

import pytest

from aucove import _meta
from aucove.domain import compat
from aucove.domain.compat import Compat, check, parse_specifiers, parse_version, same_version, satisfies

packaging_version = pytest.importorskip("packaging.version")
packaging_specifiers = pytest.importorskip("packaging.specifiers")

try:  # hypothesis es de desarrollo; sin él solo corren las tablas
    from hypothesis import example, given, settings
    from hypothesis import strategies as st
except ImportError:  # pragma: no cover
    given = None  # type: ignore[assignment]


# -- check (01 §6.3) --------------------------------------------------------------


def test_check_ok_when_installed_equals_pin() -> None:
    assert check("0.1.3", "0.1", ">=0.1.3,<0.2", "0.1.3") == Compat(level="ok", code=None)


def test_check_warning_inside_range_with_other_pin() -> None:
    assert check("0.1.4", "0.1", ">=0.1.3,<0.2", "0.1.3") == Compat(level="warning", code=None)


def test_check_ok_without_pin() -> None:
    assert check("0.1.4", "0.1", ">=0.1.3,<0.2", None).level == "ok"


def test_pin_equality_follows_pep440() -> None:
    assert check("0.1", "0.1", ">=0.1,<0.2", "0.1.0").level == "ok"


def test_check_convention_unsupported_first() -> None:
    result = check("9.9.9", "0.2", ">=0.1,<0.2", "0.1.0")
    assert result == Compat(level="error", code="E_CONFIG_CONVENTION_UNSUPPORTED")


def test_check_core_mismatch() -> None:
    assert check("0.2.0", "0.1", ">=0.1.3,<0.2", "0.1.3") == Compat(level="error", code="E_CONFIG_CORE_MISMATCH")


@pytest.mark.parametrize(
    ("installed", "core"),
    [("no-es-version", ">=0.1"), ("0.1.0", "no-es-rango"), ("0.1.0", ""), ("0.1.0", "===0.1.0")],
)
def test_check_fails_closed_on_unparsable_input(installed: str, core: str) -> None:
    assert check(installed, "0.1", core, None) == Compat(level="error", code="E_CONFIG_CORE_MISMATCH")


def test_supported_conventions_come_from_meta() -> None:
    for convention in _meta.SUPPORTED_CONVENTIONS:
        assert check(_meta.__version__, convention, f"=={_meta.__version__}", None).level == "ok"


def test_installed_version_satisfies_its_own_default_range() -> None:
    """REQ-ARCH-44: init escribe >=X.Y.Z,<X.(Y+1) con la versión instalada."""
    v = parse_version(_meta.__version__)
    assert v is not None and v.release[0] == 0
    core = f">={_meta.__version__},<0.{v.release[1] + 1}"
    assert satisfies(_meta.__version__, core)


# -- parse_version ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.0", (0, (1, 0), None, None, None, None)),
        ("v1.0", (0, (1, 0), None, None, None, None)),
        ("1!2.0", (1, (2, 0), None, None, None, None)),
        ("1.0a1", (0, (1, 0), ("a", 1), None, None, None)),
        ("1.0.alpha.1", (0, (1, 0), ("a", 1), None, None, None)),
        ("1.0-beta2", (0, (1, 0), ("b", 2), None, None, None)),
        ("1.0c3", (0, (1, 0), ("rc", 3), None, None, None)),
        ("1.0pre", (0, (1, 0), ("rc", 0), None, None, None)),
        ("1.0preview4", (0, (1, 0), ("rc", 4), None, None, None)),
        ("1.0-1", (0, (1, 0), None, 1, None, None)),
        ("1.0.rev2", (0, (1, 0), None, 2, None, None)),
        ("1.0r", (0, (1, 0), None, 0, None, None)),
        ("1.0.dev", (0, (1, 0), None, None, 0, None)),
        ("1.0rc1.post2.dev3+ubuntu.1", (0, (1, 0), ("rc", 1), 2, 3, ("ubuntu", 1))),
        ("  1.0  ", (0, (1, 0), None, None, None, None)),
    ],
)
def test_parse_version_grammar(text: str, expected: tuple[object, ...]) -> None:
    v = parse_version(text)
    assert v is not None
    assert (v.epoch, v.release, v.pre, v.post, v.dev, v.local) == expected


@pytest.mark.parametrize("text", ["", "1.", "a1", "1.0+", "1.0 1", "1..0", "1.0-dev-1-x", "1.0+ubuntu..1"])
def test_parse_version_rejects_invalid(text: str) -> None:
    assert parse_version(text) is None


def test_parse_version_rejects_non_text() -> None:
    assert parse_version(1.0) is None  # type: ignore[arg-type]


def test_version_helpers() -> None:
    v = parse_version("1.2rc1.post3+local.7")
    assert v is not None and v.is_prerelease and v.is_postrelease
    assert v.public().local is None and v.base().release == (1, 2) and v.base().pre is None
    dev = parse_version("1.2.dev1")
    assert dev is not None and dev.is_prerelease and not dev.is_postrelease


def test_same_version() -> None:
    assert same_version("1.0", "1.0.0")
    assert not same_version("1.0", "1.0.1")
    assert not same_version("x", "1.0")


# -- parse_specifiers ------------------------------------------------------------


def test_parse_specifiers_accepts_schema_grammar() -> None:
    specs = parse_specifiers(" >= 0.1.3 , < 0.2 ,!=0.1.5.*,~=0.1.3rc1.post1.dev2")
    assert specs is not None
    assert [(s.operator, s.wildcard) for s in specs] == [(">=", False), ("<", False), ("!=", True), ("~=", False)]


@pytest.mark.parametrize(
    "text",
    [
        "", "   ", ">=0.1,", "0.1", "===0.1", ">=0.1.*", "==0.1rc1.*", "~=1", ">=1!0.1",
        "==0.1+local", ">=v0.1", ">=x", "<>0.1",
    ],
)
def test_parse_specifiers_rejects_outside_schema(text: str) -> None:
    assert parse_specifiers(text) is None


def test_parse_specifiers_rejects_non_text() -> None:
    assert parse_specifiers(None) is None  # type: ignore[arg-type]


# -- tabla de casos frente a packaging ----------------------------------------------

CASES: list[tuple[str, str]] = [
    ("0.1.3", ">=0.1.3,<0.2"),
    ("0.1.2", ">=0.1.3,<0.2"),
    ("0.2.0", ">=0.1.3,<0.2"),
    ("0.2.0rc1", ">=0.1.3,<0.2"),
    ("0.2.0.dev1", "<0.2"),
    ("0.1.9.dev1", "<0.2"),
    ("0.2rc1", "<0.2rc2"),
    ("0.2rc1", "<0.2rc1"),
    ("0.1.3.post1", ">0.1.3"),
    ("0.1.4.post1", ">0.1.3"),
    ("0.1.3.post2", ">0.1.3.post1"),
    ("0.1.3+local", ">0.1.3"),
    ("0.1.4+local", ">0.1.3"),
    ("0.1.3+local", "==0.1.3"),
    ("0.1.3+local", "<=0.1.3"),
    ("0.1.3+local", ">=0.1.3"),
    ("0.1.3+local", "<0.1.4"),
    ("0.1", "==0.1.0"),
    ("0.1.0", "==0.1"),
    ("0.1.5", "==0.1.*"),
    ("0.2", "==0.1.*"),
    ("0.1", "==0.1.0.*"),
    ("0", "==0.0.*"),
    ("0.1rc1", "==0.1.*"),
    ("0.1.0.post1", "==0.1.0.*"),
    ("1!0.1", "==0.1.*"),
    ("0.1.5", "!=0.1.*"),
    ("0.1.5", "!=0.1.5"),
    ("2.2.5", "~=2.2"),
    ("3.0", "~=2.2"),
    ("2.2.post3", "~=2.2.post3"),
    ("2.2", "~=2.2.post3"),
    ("1.4.5", "~=1.4.5a4"),
    ("1.4.5a5", "~=1.4.5a4"),
    ("1.5.0", "~=1.4.5a4"),
    ("0.1.0a1", ">=0.1.0a1,<0.1.0b1"),
    ("0.1.0b1", ">=0.1.0a1,<0.1.0b1"),
    ("0.1.0.dev0", ">=0.1.0.dev0"),
    ("0.1.0", "<=0.1"),
    ("0.1.1", "<=0.1"),
    ("1!0.1", ">=0.1"),
    ("1!0.1", "<0.2"),
    ("0.1.0", "> 0.0.9 , != 0.1.0"),
]


@pytest.mark.parametrize(("version", "spec"), CASES)
def test_table_matches_packaging(version: str, spec: str) -> None:
    expected = packaging_specifiers.SpecifierSet(spec).contains(version, prereleases=True)
    assert satisfies(version, spec) is expected


# ``<V`` y ``>V`` en packaging 26.3 (``_ranges.standard_ranges``): excluyen solo la familia de V,
# no las pre y post de toda su release base como 26.0. Contraejemplos de la prueba diferencial y
# los bordes de cada caso, con el resultado esperado escrito.
EXCLUSIVE_CASES: list[tuple[str, str, bool]] = [
    # <V con V postrelease: el borde es V.dev0.
    ("1a0", "<1.post0", True),
    ("1.dev0", "<1.post0", True),
    ("1.post0.dev0", "<1.post1", True),
    ("1.post1.dev0", "<1.post1", False),
    ("1a0", "<1", False),
    # >V.devN y >V.postN: fuera solo V y V+local.
    ("1+abc", ">1.dev0", True),
    ("1.post0", ">1.dev0", True),
    ("1.dev0+abc", ">1.dev0", False),
    ("1.post2+abc", ">1.post1", True),
    ("1.post1+abc", ">1.post1", False),
    ("1+abc", ">1a1.post0", True),
    # >V con V final o prerrelease: fuera V+local y V.postN (misma release y misma pre).
    ("1.post0", ">1a1", True),
    ("1b0.post0", ">1a1", True),
    ("1+abc", ">1a1", True),
    ("1a1.post0", ">1a1", False),
    ("1a1+abc", ">1a1", False),
    ("1.post0", ">1", False),
    ("1+abc", ">1", False),
    # Conjuntos: packaging interseca rangos; aquí es la conjunción de los especificadores.
    ("1a0", ">=0.9,<1.post0", True),
    ("1.post0", ">1.dev0,<1.post1", True),
]


@pytest.mark.parametrize(("version", "spec", "expected"), EXCLUSIVE_CASES)
def test_exclusive_operators_follow_packaging(version: str, spec: str, expected: bool) -> None:
    assert packaging_specifiers.SpecifierSet(spec).contains(version, prereleases=True) is expected
    assert satisfies(version, spec) is expected


def test_satisfies_rejects_invalid_inputs() -> None:
    assert satisfies("x", ">=0.1") is False
    assert satisfies("0.1", ">=x") is False


def test_order_matches_packaging_on_a_sorted_list() -> None:
    raw = [
        "1.0.dev0", "1.0a1.dev1", "1.0a1", "1.0a2", "1.0b1", "1.0rc1", "1.0", "1.0+abc", "1.0+abc.2",
        "1.0+1", "1.0.post0.dev1", "1.0.post0", "1.0.post1", "1.0.1", "1.1.dev1", "1!0.1",
    ]
    ours = sorted(raw, key=lambda s: parse_version(s).key())  # type: ignore[union-attr]
    theirs = sorted(raw, key=packaging_version.Version)
    assert ours == theirs


# -- comparación diferencial con Hypothesis ------------------------------------------

if given is not None:
    _nums = st.integers(min_value=0, max_value=3)
    _release = st.lists(_nums, min_size=1, max_size=4).map(lambda r: ".".join(map(str, r)))
    _pre = st.one_of(st.just(""), st.tuples(st.sampled_from(["a", "b", "rc"]), _nums).map(lambda p: f"{p[0]}{p[1]}"))
    _post = st.one_of(st.just(""), _nums.map(lambda n: f".post{n}"))
    _dev = st.one_of(st.just(""), _nums.map(lambda n: f".dev{n}"))
    _local = st.one_of(st.just(""), st.sampled_from(["+abc", "+1", "+abc.2", "+2.x"]))
    _epoch = st.one_of(st.just(""), st.just(""), st.just("1!"))
    versions = st.builds(lambda e, r, p, po, d, lo: f"{e}{r}{p}{po}{d}{lo}", _epoch, _release, _pre, _post, _dev, _local)

    _spec_version = st.builds(lambda r, p, po, d: f"{r}{p}{po}{d}", _release, _pre, _post, _dev)
    _plain = st.tuples(st.sampled_from(["~=", "==", "!=", "<=", ">=", "<", ">"]), _spec_version).map(
        lambda x: f"{x[0]}{x[1]}"
    )
    _wild = st.tuples(st.sampled_from(["==", "!="]), _release).map(lambda x: f"{x[0]}{x[1]}.*")
    one_spec = st.one_of(_plain, _wild)
    spec_sets = st.lists(one_spec, min_size=1, max_size=3).map(",".join)

    # Contraejemplos mínimos que dio esta prueba, con varias semillas, antes de seguir a packaging 26.3.
    @settings(database=None, max_examples=600, deadline=None)
    @given(version=versions, spec=spec_sets)
    @example(version="1a0", spec="<1.post0")
    @example(version="0.dev0", spec="<0.post0")
    @example(version="0.0.1a0", spec="<0.0.1.post0")
    @example(version="1+abc", spec=">1.dev0")
    @example(version="1.0.0.1+abc", spec=">1.0.0.1.dev0")
    @example(version="1+abc", spec="==1,>1.dev0")
    @example(version="0.0.1+abc", spec="!=0,>0.0.1.dev0")
    def test_satisfies_matches_packaging(version: str, spec: str) -> None:
        try:
            spec_set = packaging_specifiers.SpecifierSet(spec)
        except packaging_specifiers.InvalidSpecifier:
            assert parse_specifiers(spec) is None
            return
        assert parse_specifiers(spec) is not None
        assert satisfies(version, spec) is spec_set.contains(version, prereleases=True)

    @settings(database=None, max_examples=400, deadline=None)
    @given(a=versions, b=versions)
    def test_order_matches_packaging(a: str, b: str) -> None:
        va, vb = parse_version(a), parse_version(b)
        assert va is not None and vb is not None
        pa, pb = packaging_version.Version(a), packaging_version.Version(b)
        assert (va.key() < vb.key()) is (pa < pb)
        assert (va.key() == vb.key()) is (pa == pb)


def test_module_is_stdlib_only() -> None:
    """01 §3.1: domain no importa packaging (lo comprueba también test_layers)."""
    with open(compat.__file__, encoding="utf-8") as fh:
        source = fh.read()
    assert "import packaging" not in source and "from packaging" not in source
