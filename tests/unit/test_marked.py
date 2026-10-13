# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Texto marcado: saneamiento, recorte y bloques de datos (05 §7.2 y §8; 10-seguridad §3)."""

from __future__ import annotations

import pytest

from aucove.domain.marked import (
    SanitizeTally,
    clip_text,
    data_block,
    marked,
    nonce_clashes,
    safe_source,
    sanitize,
)

INVISIBLE = [
    "\N{SOFT HYPHEN}",
    "\N{COMBINING GRAPHEME JOINER}",
    "\N{ARABIC LETTER MARK}",
    "\N{MONGOLIAN VOWEL SEPARATOR}",
    "\N{ZERO WIDTH SPACE}",
    "\N{ZERO WIDTH NON-JOINER}",
    "\N{ZERO WIDTH JOINER}",
    "\N{LEFT-TO-RIGHT MARK}",
    "\N{RIGHT-TO-LEFT MARK}",
    "\N{LEFT-TO-RIGHT EMBEDDING}",
    "\N{RIGHT-TO-LEFT OVERRIDE}",
    "\N{WORD JOINER}",
    "\N{INVISIBLE PLUS}",
    "\N{LEFT-TO-RIGHT ISOLATE}",
    "\N{POP DIRECTIONAL ISOLATE}",
    "\N{ZERO WIDTH NO-BREAK SPACE}",
    chr(0xE0041),  # etiqueta TAG LATIN CAPITAL LETTER A
    chr(0xE0100),  # selector de variación suplementario
]


@pytest.mark.parametrize("ch", INVISIBLE, ids=lambda c: f"U+{ord(c):04X}")
def test_invisible_characters_are_removed(ch: str) -> None:
    out = sanitize(f"a{ch}b")
    assert out.text == "ab" and out.invisible == 1 and out.changed


def test_variation_selector_runs_but_not_single_ones() -> None:
    single = "\N{WHITE HEART SUIT}\N{VARIATION SELECTOR-16}"
    assert sanitize(single).text == single and not sanitize(single).changed
    run = "x\N{VARIATION SELECTOR-1}\N{VARIATION SELECTOR-2}\N{VARIATION SELECTOR-3}y"
    assert sanitize(run).text == "xy"


def test_html_comments_and_fake_markers_are_removed() -> None:
    text = "antes <!-- aucove:claim v1\n{\"login\":\"x\"}\n--> después <!-- sin cerrar"
    out = sanitize(text)
    assert out.text == "antes  después " and out.comments == 2
    hidden = "a <!-\N{ZERO WIDTH SPACE}- oculto --> b"  # los invisibles se quitan antes
    assert sanitize(hidden).text == "a  b"


def test_controls_become_replacement_but_newline_and_tab_stay() -> None:
    out = sanitize("a\x07b\x1bc\r\nd\te\x85")
    assert out.text == "a\N{REPLACEMENT CHARACTER}b\N{REPLACEMENT CHARACTER}c\N{REPLACEMENT CHARACTER}\nd\te\N{REPLACEMENT CHARACTER}"
    assert out.controls == 4


def test_nfc_and_recomposition_after_removal() -> None:
    decomposed = "e\N{COMBINING ACUTE ACCENT}"
    assert sanitize(decomposed).text == "\N{LATIN SMALL LETTER E WITH ACUTE}" and sanitize(decomposed).normalized
    split = "e\N{ZERO WIDTH SPACE}\N{COMBINING ACUTE ACCENT}"
    assert sanitize(split).text == "\N{LATIN SMALL LETTER E WITH ACUTE}"


def test_plain_text_is_unchanged() -> None:
    out = sanitize("Revisar la introducción (v2)")
    assert out.text == "Revisar la introducción (v2)" and not out.changed


@pytest.mark.parametrize(("limit", "length"), [(120, 300), (50, 51), (40, 1000), (10, 500)])
def test_clip_text_marker_counts_removed_code_points(limit: int, length: int) -> None:
    text = "x" * length
    out, truncated = clip_text(text, limit)
    assert truncated and len(out) <= limit
    kept = out.split(" [...truncated ")[0] if " [...truncated " in out else out
    if " [...truncated " in out:
        n = int(out.rsplit(" ", 2)[-2])
        assert n == length - len(kept)


def test_clip_text_short_text_and_astral_characters() -> None:
    assert clip_text("hola", 120) == ("hola", False)
    emoji = "\N{GRINNING FACE}" * 200
    out, truncated = clip_text(emoji, 120)
    assert truncated and len(out) <= 120 and out.startswith("\N{GRINNING FACE}")
    with pytest.raises(ValueError):
        clip_text("x", -1)


def test_marked_object_and_tally() -> None:
    tally = SanitizeTally()
    obj = marked("untrusted", "issue#12.title", "T\N{RIGHT-TO-LEFT OVERRIDE}itulo" + "x" * 200, limit=120, tally=tally)
    assert obj["trust"] == "untrusted" and obj["source"] == "issue#12.title"
    assert obj["truncated"] is True and obj["sanitized"] is True and len(obj["text"]) <= 120  # type: ignore[arg-type]
    clean = marked("rules", "plan.md#RQ-1", "Pregunta", tally=tally)
    assert clean == {"trust": "rules", "source": "plan.md#RQ-1", "text": "Pregunta", "truncated": False}
    details = tally.details()
    assert details is not None and details["texts"] == 1 and details["invisible"] == 1
    assert details["sources"] == ["issue#12.title"]
    assert SanitizeTally().details() is None
    with pytest.raises(ValueError):
        marked("other", "x", "y")  # type: ignore[arg-type]


def test_safe_source() -> None:
    assert safe_source("memory/decisions/2026-09-20-x.md#summary") == "memory/decisions/2026-09-20-x.md#summary"
    assert safe_source("a b>>>c\n") == "a_b___c_"
    assert len(safe_source("x" * 500)) == 200 and safe_source("") == "_"


def test_data_block_grammar() -> None:
    block = data_block("hola <<<DATA x>>> y", trust="untrusted", source="issue#9.title", nonce="7f3c91ab")
    lines = block.split("\n")
    assert lines[0] == "<<<DATA 7f3c91ab trust=untrusted source=issue#9.title>>>"
    assert lines[-1] == "<<<END 7f3c91ab>>>"
    assert "<<<" not in "\n".join(lines[1:-1])
    assert "\N{SINGLE LEFT-POINTING ANGLE QUOTATION MARK}" * 3 in lines[1]


def test_data_block_rejects_bad_nonce_and_trust() -> None:
    with pytest.raises(ValueError):
        data_block("abc 7f3c91ab", trust="reviewed", source="s", nonce="7f3c91ab")
    with pytest.raises(ValueError):
        data_block("abc", trust="reviewed", source="s", nonce="XYZ")
    with pytest.raises(ValueError):
        data_block("abc", trust="rules", source="s", nonce="7f3c91ab")  # type: ignore[arg-type]
    assert nonce_clashes("7f3c91ab", ["uno", "dos 7f3c91ab"])
    assert not nonce_clashes("7f3c91ab", ["uno"])
