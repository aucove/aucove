# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``conv.gitignore.required`` exige los ocho patrones de secretos de REQ-SEC-35.

10-seguridad REQ-SEC-35 (DEBE) pide que el ``.gitignore`` traiga ``.env``, ``.env.*``,
``*.pem``, ``*.key``, ``*.p12``, ``*.pfx``, ``id_rsa*`` e ``id_ed25519*``. La plantilla
ya los trae (``test_template_gitignore_secrets``), pero ``GITIGNORE_REQUIRED`` copiaba el bloque
``[req] secretos`` de 02-convencion §9, que solo tiene los tres primeros. Un proyecto
que borrara ``*.key``, ``*.p12``, ``*.pfx``, ``id_rsa*`` o ``id_ed25519*`` de su
``.gitignore`` pasaba ``validate`` sin aviso, y una clave SSH o un certificado PKCS#12
podía entrar al commit. Ahora la regla los exige, en el orden de REQ-SEC-35, y la
plantilla renderizada en ``es`` y en ``en`` sigue sin hallazgos.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from aucove.i18n import Lang, translator
from aucove.ops.rules import convention
from tests.conftest import load_tool
from tests.unit.test_rule_convention import evaluate, of, run_on_render, sub

CODE = "conv.gitignore.required"
# Patrones de secretos de REQ-SEC-35, en el orden del requisito.
SEC35_SECRETS: tuple[str, ...] = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*")
# Los cinco que faltaban en la regla.
NEW_SECRETS: tuple[str, ...] = SEC35_SECRETS[3:]
LANGS: tuple[Lang, ...] = ("es", "en")


@pytest.fixture(scope="module")
def render() -> Mapping[str, Mapping[str, bytes]]:
    tool = load_tool("render_template")
    return {lang: tool.render_files(lang) for lang in LANGS}


def _without(*entries: str) -> Mapping[str, object]:
    """Cambio que quita del ``.gitignore`` de la plantilla cada línea de ``entries``."""

    def change(text: str) -> str:
        lines = text.split("\n")
        for entry in entries:
            assert entry in lines, entry
        return "\n".join(line for line in lines if line not in entries)

    return {".gitignore": change}


def _message(lang: Lang, entry: str) -> str:
    return translator(lang)(f"finding.{CODE}.message", entry=entry)


def test_rule_requires_every_req_sec_35_secret_in_order() -> None:
    required = convention.GITIGNORE_REQUIRED
    for entry in SEC35_SECRETS:
        assert entry in required, entry
    assert tuple(e for e in required if e in SEC35_SECRETS) == SEC35_SECRETS
    # El bloque [req] secretos cierra la lista, como en 02 §9.
    assert required[-len(SEC35_SECRETS) :] == SEC35_SECRETS


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("entry", NEW_SECRETS)
def test_missing_secret_pattern_is_reported(render: Mapping[str, Mapping[str, bytes]], lang: Lang, entry: str) -> None:
    findings = run_on_render(render, _without(entry), lang=lang)  # type: ignore[arg-type]
    [finding] = of(findings, CODE)
    assert finding.path == ".gitignore"
    assert finding.severity == "error"
    assert finding.message == _message(lang, entry)
    assert [f.code for f in findings] == [CODE]


@pytest.mark.parametrize("lang", LANGS)
def test_all_new_secret_patterns_missing_reported_in_req_order(
    render: Mapping[str, Mapping[str, bytes]], lang: Lang
) -> None:
    findings = of(run_on_render(render, _without(*NEW_SECRETS), lang=lang), CODE)  # type: ignore[arg-type]
    assert [f.message for f in findings] == [_message(lang, e) for e in NEW_SECRETS]


@pytest.mark.parametrize("lang", LANGS)
def test_template_gitignore_has_no_findings(render: Mapping[str, Mapping[str, bytes]], lang: Lang) -> None:
    assert run_on_render(render, lang=lang) == []
    assert evaluate(render[lang], lang=lang) == []


@pytest.mark.parametrize("lang", LANGS)
def test_commented_or_variant_line_does_not_count(render: Mapping[str, Mapping[str, bytes]], lang: Lang) -> None:
    """La línea debe estar tal cual (REQ-CONV-81): comentada o con otra forma no cuenta."""
    changes = {".gitignore": sub("\nid_rsa*\n", "\n# id_rsa*\n/id_rsa*\n")}
    [finding] = of(run_on_render(render, changes, lang=lang), CODE)  # type: ignore[arg-type]
    assert finding.message == _message(lang, "id_rsa*")
