# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Lector mínimo de ``.bib`` (``aucove.domain.bibtex``; 02-convencion §5).

Lee entradas sin interpretar macros ni ejecutar nada, se recupera de los errores
de sintaxis en la siguiente entrada y tiene tiempo acotado (09-ci REQ-CI-24).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from aucove.domain.bibtex import BibEntry, BibError, BibFile, parse_bib
from aucove.domain.latex import Deadline, ScanTimeout

BIB = Path(__file__).resolve().parent.parent / "fixtures" / "latex" / "bib"


def read(name: str) -> str:
    return (BIB / name).read_text(encoding="utf-8")


# -- entradas válidas ---------------------------------------------------------------------


def test_valid_fixture_entries_lines_and_fields() -> None:
    bib = parse_bib(read("valid.bib"))
    assert bib.errors == ()
    assert [(e.entry_type, e.key, e.line) for e in bib.entries] == [
        ("article", "alpha2020", 8),
        ("book", "beta:2019-b", 16),
        ("misc", "gamma_3", 21),
    ]
    alpha = bib.entries[0]
    # Nombres de campo en minúsculas; valores tal cual (llaves, comillas y concatenación).
    assert alpha.fields["title"] == "{Nested {Braces} and \\{escaped\\} ones}"
    assert alpha.fields["author"] == '"Alpha, Ann and Beta, Bob"'
    assert alpha.fields["journal"] == 'jt # " (special issue)"'
    assert alpha.fields["year"] == "2020"
    assert alpha.field_lines == {"title": 9, "author": 10, "journal": 11, "year": 12}
    # Un campo repetido conserva el primero.
    assert "Second" not in alpha.fields["title"]
    beta = bib.entries[1]
    assert beta.fields["title"] == '"Quoted {with "inner" quote} value"'
    assert bib.keys() == frozenset({"alpha2020", "beta:2019-b", "gamma_3"})


def test_skipped_types_and_comment_lines_are_not_entries() -> None:
    text = (
        "% @book{inComment, title = {x}}\n"
        "@string{ s = {x} }\n"
        "@STRING(t = \"y\")\n"
        "@preamble{ \"\\def\\x{1}\" }\n"
        "@Comment{ @misc{fake, } }\n"
        "free text between entries is a comment\n"
        "@misc{real, note = {n}}\n"
    )
    bib = parse_bib(text)
    assert [e.key for e in bib.entries] == ["real"]
    assert bib.errors == ()


def test_stray_at_sign_is_a_syntax_error_like_biber() -> None:
    bib = parse_bib("note: write to me@example.org\n@misc{k}\n")
    assert [e.key for e in bib.entries] == ["k"]
    assert bib.errors == (BibError(1, "syntax"),)


def test_comments_between_fields() -> None:
    text = "@misc{k,\n  % a comment line with } and {\n  note = {n}, % trailing\n  year = 2020\n}\n"
    entry = parse_bib(text).entries[0]
    assert dict(entry.fields) == {"note": "{n}", "year": "2020"}
    assert entry.field_lines == {"note": 3, "year": 4}


def test_parenthesized_entry_and_spaces_after_at() -> None:
    bib = parse_bib("@ book ( k1 , title = {T} )\n@misc{k2}\n")
    assert [(e.entry_type, e.key) for e in bib.entries] == [("book", "k1"), ("misc", "k2")]
    assert bib.entries[1].fields == {}


def test_entries_mapping_is_read_only() -> None:
    entry = parse_bib("@misc{k, a = {1}}").entries[0]
    with pytest.raises(TypeError):
        entry.fields["a"] = "2"  # type: ignore[index]


def test_by_key_keeps_the_first_entry_like_biber() -> None:
    bib = parse_bib("@misc{dup, note = {first}}\n@misc{dup, note = {second}}\n")
    assert len(bib.entries) == 2
    assert bib.by_key()["dup"].fields["note"] == "{first}"


def test_canonical_ignores_whitespace_and_field_order() -> None:
    a = parse_bib("@book{k,\n  title = {A  B},\n  year = 2020\n}").entries[0]
    b = parse_bib("@Book{k, year = 2020, title = {A B}}").entries[0]
    c = parse_bib("@book{k, title = {A C}, year = 2020}").entries[0]
    assert a.canonical() == b.canonical()
    assert a.canonical() != c.canonical()


def test_entry_defaults() -> None:
    e = BibEntry(entry_type="misc", key="k", line=1)
    assert e.fields == {} and e.field_lines == {}
    assert BibFile(entries=(e,), errors=()).by_key() == {"k": e}


# -- errores y recuperación ---------------------------------------------------------------


def test_broken_fixture_recovers_at_each_entry() -> None:
    bib = parse_bib(read("broken.bib"))
    assert [e.key for e in bib.entries] == ["good2021", "last2022"]
    assert bib.errors == (
        BibError(3, "unterminated"),
        BibError(7, "missing-key"),
        BibError(11, "syntax"),
        BibError(15, "syntax"),
        BibError(25, "unterminated"),
    )


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("@{nokind, a = {1}}", "syntax"),
        ("@book no brace", "syntax"),
        ("@book{k, = {1}}", "syntax"),
        ("@book{k, a = , b = {2}}", "syntax"),
        ("@book{k a = {1}}", "missing-key"),
        ("@book{k, a = {1}", "unterminated"),
        ("@book{k, a = {1", "unterminated"),
        ('@book{k, a = "1', "unterminated"),
        ("@book{k, a = ", "unterminated"),
        ("@book{k, a", "syntax"),
        ("@book{k, a = 1 # ", "unterminated"),
        ("@string(x = {1}", "unterminated"),
        ("@comment{never closed", "unterminated"),
        ("@book", "syntax"),
    ],
)
def test_syntax_error_kinds(text: str, kind: str) -> None:
    bib = parse_bib(text)
    assert bib.entries == ()
    assert [e.kind for e in bib.errors] == [kind]
    assert bib.errors[0].line == 1


def test_unterminated_value_stops_at_next_entry_start() -> None:
    text = '@misc{a, title = "open\n@misc{b, title = {ok}}\n@misc{c, note = {x\n@misc{d}\n'
    bib = parse_bib(text)
    assert [e.key for e in bib.entries] == ["b", "d"]
    assert [(e.line, e.kind) for e in bib.errors] == [(1, "unterminated"), (3, "unterminated")]


def test_at_inside_a_line_is_not_a_recovery_point() -> None:
    # Tras un error, solo se retoma en una línea que empieza con @tipo{ o @tipo(.
    text = "@misc{a, title = {open\nmail me at x@y{z} please\n@ not an entry\n@word only\n@misc{b}\n"
    bib = parse_bib(text)
    assert [e.key for e in bib.entries] == ["b"]
    assert bib.errors == (BibError(1, "unterminated"),)


def test_escaped_quote_inside_quoted_value() -> None:
    entry = parse_bib('@misc{k, title = "a \\" b"}').entries[0]
    assert entry.fields["title"] == '"a \\" b"'


def test_max_entries_limit() -> None:
    text = "".join(f"@misc{{k{i}}}\n" for i in range(10))
    assert len(parse_bib(text, max_entries=3).entries) == 3


# -- tiempo acotado -----------------------------------------------------------------------


def test_deadline_raises_scan_timeout() -> None:
    ticks = iter(range(10**9))

    def clock() -> float:
        return float(next(ticks))  # cada consulta avanza un segundo

    text = "".join(f"@misc{{k{i}, note = {{x}}}}\n" for i in range(5000))
    with pytest.raises(ScanTimeout):
        parse_bib(text, deadline=Deadline(10.0, clock=clock))


def test_large_pathological_input_is_linear() -> None:
    # Llaves sin cerrar y arrobas por todas partes: no hay retroceso catastrófico.
    text = "@misc{k, t = {" + "{" * 200_000 + "\n" + "@x" * 100_000
    start = time.monotonic()
    bib = parse_bib(text)
    assert time.monotonic() - start < 10
    assert bib.entries == ()
    assert bib.errors and bib.errors[0].kind == "unterminated"
