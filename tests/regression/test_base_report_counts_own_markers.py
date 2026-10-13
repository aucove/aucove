# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``base-report`` solo cuenta sus propios marcadores ``op`` (09-ci REQ-CI-32, REQ-CI-34).

Los ``op_id`` de base-report son predecibles: salen del SHA, de la URL del run o del número
del Issue, que son públicos en cuanto empieza la ejecución. En un repo público cualquier
cuenta puede comentar en el Issue ``main-broken`` con ese marcador antes de que termine la
compilación. Si base-report contara ese comentario ajeno como propio, concluiría
``unchanged`` y no publicaría la falla del nuevo SHA, o cerraría el Issue sin el comentario
«resuelto por». La recuperación del adaptador también busca solo comentarios propios
(08-forja §4.7, REQ-FORGE-35), y el texto ajeno no es confiable (10-seguridad).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from aucove import _meta
from aucove.forge.fake import CI_LOGIN
from aucove.ops import ci_base_report as br

from tests.unit.test_ci_base_report import MARKER, RUN_URL, argv_for, invoke, make_world, run

ATTACKER = "mallory"
RUN_URL_2 = RUN_URL.replace("101", "102")


def forged_marker(prefix: str, *parts: object) -> str:
    """El marcador que un tercero puede calcular con datos públicos (05 §6.1, 00-marco §5.14)."""
    key = hashlib.sha256("\n".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]
    return f"<!-- {_meta.NAME}:op v1 id={prefix}-{key} -->"


def bot_comments(forge: object, number: int) -> list[str]:
    return [c.body for c in forge.list_comments(number) if c.author == CI_LOGIN]  # type: ignore[attr-defined]


def test_foreign_failure_marker_does_not_suppress_the_new_report(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.forge.seed_user(ATTACKER)
    code, out = run(w, w.c2, build="failure")
    assert code == 0 and out["action"] == "created"
    number = int(out["issue"])

    # Mientras compila c3, un tercero publica el marcador de esa ejecución.
    w.forge.seed_comment(number, author=ATTACKER, body="ya lo vi " + forged_marker("base-report-fail", w.c3, RUN_URL_2))

    code, out = run(w, w.c3, build="failure", run_url=RUN_URL_2)
    assert code == 0 and out == {"status": "broken", "action": "commented", "issue": str(number)}
    mine = bot_comments(w.forge, number)
    assert len(mine) == 1 and f"`{w.c3}`" in mine[0] and RUN_URL_2 in mine[0]

    # Un re-run del mismo job sí reconoce su propio comentario.
    code, out = run(w, w.c3, build="failure", run_url=RUN_URL_2)
    assert code == 0 and out["action"] == "unchanged"
    assert len(bot_comments(w.forge, number)) == 1


def test_foreign_resolve_marker_does_not_suppress_resolved_by(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.forge.seed_user(ATTACKER)
    _code, out = run(w, w.c2, build="failure")
    number = int(out["issue"])
    w.forge.seed_comment(number, author=ATTACKER, body=forged_marker("base-report-resolve", number, w.c3))

    code, out = run(w, w.c3, run_url=RUN_URL_2)
    assert code == 0 and out == {"status": "green", "action": "closed", "issue": str(number)}
    issue = w.forge.get_issue(number)
    assert issue is not None and issue.state == "closed"
    mine = bot_comments(w.forge, number)
    assert len(mine) == 1 and mine[0].startswith(f"Resuelto por `{w.c3}`.")


def test_foreign_duplicate_marker_does_not_suppress_the_duplicate_note(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.forge.seed_user(ATTACKER)
    first = w.forge.seed_issue(title="a", body=f"{MARKER}\nviejo", labels=[br.LABEL], author=CI_LOGIN)
    second = w.forge.seed_issue(title="b", body=f"{MARKER}\notro", labels=[br.LABEL], author=CI_LOGIN)
    w.forge.seed_comment(second.number, author=ATTACKER,
                         body=forged_marker("base-report-duplicate", second.number, first.number))

    code, out = run(w, w.c1, build="failure")
    assert code == 0 and out["issue"] == str(first.number)
    mine = bot_comments(w.forge, second.number)
    assert len(mine) == 1 and mine[0].startswith(f"Duplicado de #{first.number}:")
    dup = w.forge.get_issue(second.number)
    assert dup is not None and dup.state == "closed" and dup.state_reason == "duplicate"


def test_open_marker_in_a_foreign_issue_body_is_not_ours(tmp_path: Path) -> None:
    # Un Issue con la etiqueta y el marcador, pero creado por otra cuenta, que trae en el
    # cuerpo el marcador ``op`` de apertura de esta ejecución: no es el reporte de base-report.
    w = make_world(tmp_path)
    body = f"{MARKER}\n" + forged_marker("base-report-open", w.c1, RUN_URL)
    issue = w.forge.seed_issue(title="a", body=body, labels=[br.LABEL], author="ana")

    code, out = run(w, w.c1, build="failure")
    assert code == 0 and out == {"status": "broken", "action": "commented", "issue": str(issue.number)}
    assert len(bot_comments(w.forge, issue.number)) == 1


def test_own_markers_count_when_the_token_is_not_the_ci_bot(tmp_path: Path) -> None:
    # Con otro token (p. ej. un PAT fuera de Actions) cuentan los comentarios de esa cuenta.
    w = make_world(tmp_path)
    forge = w.forge.as_user("ana")
    issue = w.forge.seed_issue(title="a", body=MARKER, labels=[br.LABEL], author="ana")
    code, out = invoke(argv_for(w.policy, w.c1, build="failure"), forge)
    assert code == 0 and "action=commented" in out
    code, out = invoke(argv_for(w.policy, w.c1, build="failure"), forge)
    assert code == 0 and "action=unchanged" in out
    assert [c.author for c in w.forge.list_comments(issue.number)] == ["ana"]
