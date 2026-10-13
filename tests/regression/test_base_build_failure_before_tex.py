# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Una compilación de la base que falla antes del paso TeX abre el Issue (09-ci REQ-CI-32).

``main-broken.yml`` pasa ``BUILD=needs.compile.outputs.outcome``, que es ``steps.tex.outcome``
de ``compile.yml`` (09-ci §3.2 y §7.1). Si falla un paso anterior (``ci build-plan`` con un
valor inválido, el ``docker pull`` de la imagen o la instalación del núcleo), el paso ``tex``
no corre y su ``outcome`` es ``skipped``, no ``''``. En modo ``base``, build-plan siempre emite
``run=true``, así que ese ``skipped`` no es una omisión legítima: la compilación falló y
base-report DEBE abrir o comentar el Issue (REQ-CI-32; fallo cerrado, REQ-CI-8). La única
excepción es una ejecución cancelada, que se ve en ``--validate cancelled``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aucove.ops import ci_base_report as br

from tests.unit.test_ci_base_report import make_world, run


@pytest.mark.parametrize("validate", ["success", "failure", "skipped"])
def test_skipped_build_counts_as_failure(validate: str) -> None:
    assert br.classify("skipped", validate) == "broken"


def test_skipped_build_of_a_cancelled_run_is_inconclusive() -> None:
    assert br.classify("skipped", "cancelled") == "inconclusive"


def test_skipped_build_opens_the_issue_with_build_compile_as_failed_check(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    code, out = run(w, w.c3, build="skipped", validate="success")
    assert code == 0 and out["status"] == "broken" and out["action"] == "created"
    body = w.forge.get_issue(int(out["issue"])).body  # type: ignore[union-attr]
    assert f"`{br.BUILD_CHECK}`" in body and br.VALIDATE_CHECK not in body
    assert "La compilación no llegó a ejecutarse" in body
    assert w.forge.list_issues(labels=[br.LABEL], state="open")[0].assignees == ("ana", "bruno")


def test_skipped_build_comments_on_the_open_issue(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    _code, out = run(w, w.c2, build="failure")
    number = int(out["issue"])
    code, out = run(w, w.c3, build="skipped", validate="success", run_url="https://github.com/example-org/demo/actions/runs/102")
    assert code == 0 and out == {"status": "broken", "action": "commented", "issue": str(number)}
    comment = w.forge.list_comments(number)[-1].body
    assert f"`{w.c3}`" in comment and f"`{br.BUILD_CHECK}`" in comment


def test_skipped_build_does_not_close_the_issue(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    _code, out = run(w, w.c2, build="failure")
    number = int(out["issue"])
    run(w, w.c3, build="skipped", validate="success", run_url="https://github.com/example-org/demo/actions/runs/103")
    issue = w.forge.get_issue(number)
    assert issue is not None and issue.state == "open"
