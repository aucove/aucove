# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``secrets`` y los commits de fusión.

09-ci §4.1 y REQ-SEC-36 fijan ``gitleaks git --log-opts="<merge-base>..<head_sha>"``.
gitleaks analiza la salida de ``git log -p -U0 <log-opts>``, y ``git log -p`` no
muestra diff para un commit con dos o más padres salvo que se pida (``-m``, ``--cc`` o
``--diff-merges``). Por eso lo que se agregaba al resolver una fusión dentro de la rama
de la tarea (un archivo nuevo durante ``git merge --no-commit`` o la resolución de un
conflicto, que es el flujo normal de ``update``, 05-operaciones) llegaba a ``head`` sin
pasar por gitleaks. Ahora ``--log-opts`` incluye ``--diff-merges=first-parent``: el
commit de fusión muestra su diff contra la rama de la tarea.

Las pruebas usan el doble ``tests/fixtures/tools/fake_gitleaks.py``, que lanza
``git log -p -U0`` con el valor de ``--log-opts`` igual que gitleaks 8.30.1
(``sources/git.go``).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aucove.i18n import translator
from aucove.ops.rules import secrets
from aucove.ops.rules.base import RuleInput
from aucove.vcs.tree import git_tree
from tests.conftest import FIXTURES, GitRepo

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
FAKE_GITLEAKS = (sys.executable, str(FIXTURES / "tools" / "fake_gitleaks.py"))
# El patrón del doble se arma por partes para que ningún escáner real lo vea aquí.
LEAK = "FAKE" + "LEAK" + "0000000000000007"


@pytest.fixture(autouse=True)
def _clean_fake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("FAKE_GITLEAKS_LOG", "FAKE_GITLEAKS_EXIT", "FAKE_GITLEAKS_REPORT", "FAKE_GITLEAKS_SLEEP",
                 "GITLEAKS_CONFIG", "GITLEAKS_CONFIG_TOML"):
        monkeypatch.delenv(name, raising=False)


def _evil_merge(git_repo: GitRepo, *, conflict: bool) -> tuple[str, str]:
    """``task/7`` integra ``main`` con un commit de fusión que agrega un secreto.

    Devuelve ``(merge_base, head)`` como los calcula ``validate`` en modo ``pr``: el
    merge-base entre ``main`` y la cabeza, que tras la fusión es la punta de ``main``.
    """
    git_repo.commit("base", files={"project.yaml": "name: x\n", "tex/shared.tex": "% uno\n"})
    git_repo.git("checkout", "-q", "-b", "task/7")
    git_repo.commit("tarea", files={
        "sections/methods/methods.tex": "% metodos\n",
        **({"tex/shared.tex": "% tarea\n"} if conflict else {}),
    })
    git_repo.checkout("main")
    git_repo.commit("otra tarea", files={
        "sections/results/results.tex": "% resultados\n",
        **({"tex/shared.tex": "% main\n"} if conflict else {}),
    })
    git_repo.checkout("task/7")
    # Con conflicto, git merge sale con 1 y deja la fusión a medias, como en `update`.
    git_repo.git("merge", "-q", "--no-ff", "--no-commit", "main", check=not conflict)
    if conflict:
        resolved = {"tex/shared.tex": f"% resuelto\nclave = {LEAK}\n"}
    else:
        resolved = {"data/evil.txt": f"clave = {LEAK}\n"}
    head = git_repo.commit("fusion de main", files=resolved)
    assert len(git_repo.git("rev-list", "--parents", "-n", "1", head).split()) == 3  # es una fusión
    return git_repo.git("merge-base", "main", head), head


@pytest.mark.parametrize(("conflict", "path", "line"), [
    (False, "data/evil.txt", 1),  # archivo nuevo agregado durante `git merge --no-commit`
    (True, "tex/shared.tex", 2),  # secreto escrito al resolver un conflicto
])
def test_secret_added_in_a_merge_commit_is_reported(git_repo: GitRepo, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch, conflict: bool, path: str,
                                                    line: int) -> None:
    merge_base, head = _evil_merge(git_repo, conflict=conflict)
    log = tmp_path / "call.json"
    monkeypatch.setenv("FAKE_GITLEAKS_LOG", str(log))
    inp = RuleInput(mode="pr", t=translator("en"), now=NOW, tree=git_tree(git_repo.path, head),
                    base_tree=git_tree(git_repo.path, merge_base), head_sha=head,
                    tools={"gitleaks": FAKE_GITLEAKS})
    found = list(secrets.RULE.run(inp))
    assert [(f.code, f.path, f.line) for f in found] == [("E_VALIDATION_SECRET", path, line)]
    assert head[:12] in found[0].message  # el hallazgo apunta al commit de fusión
    assert LEAK not in json.dumps(found[0].to_dict())
    argv = json.loads(log.read_text(encoding="utf-8"))["argv"]
    (opts,) = [a for a in argv if a.startswith("--log-opts=")]
    assert "--diff-merges=first-parent" in opts.split("=", 1)[1].split(" ")


def test_log_opts_show_merge_diffs_against_the_task_branch() -> None:
    # Fija la forma: diff de las fusiones contra el primer padre (la rama de la tarea),
    # antes del rango, y el rango sigue siendo el último elemento.
    opts = secrets.log_opts("a" * 40, "b" * 40).split(" ")
    assert opts == ["--text", "--no-textconv", "--no-ext-diff", "--diff-merges=first-parent",
                    f"{'a' * 40}..{'b' * 40}"]
