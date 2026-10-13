# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``changed.txt`` de ``compile.yml`` incluye el origen de un renombre.

``compile.yml`` (09-ci §3.2) escribe ``changed.txt`` con ``git diff --name-only`` entre
la base (``HEAD^1`` del merge commit) y el merge commit, y ``ci build-plan --mode pr``
compila si alguna ruta es de entrada (``tex/``, ``sections/``, ``bib/``, ``project.yaml`` o
los workflows de compilación). ``git diff`` detecta renombres por defecto desde git 2.9 y
entonces solo lista el destino: un PR que movía el ``.tex`` de una parte a ``notes/``
daba ``run=false`` y ``aucove/build`` quedaba en verde sin compilar (REQ-CI-8).

La prueba ejecuta el comando ``git diff`` tal como lo escribe el workflow sobre un merge
commit real y pasa su salida a ``changes_need_build``, la decisión de ``build-plan``.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence

import pytest

from aucove.ops.ci_plan import changes_need_build
from tests.conftest import GitRepo
from tests.render.test_template_workflows import jobs, steps_of

DIFF_RE = re.compile(r"\bgit diff\b[^>\n]*")
BASE_FILES = {
    "tex/main.tex": "\\documentclass{article}\n\\input{../sections/intro/intro}\n\\input{notation}\n",
    "sections/intro/intro.tex": "Introducción con varias líneas de texto para que git la reconozca.\n" * 4,
    "tex/notation.tex": "\\newcommand{\\R}{\\mathbb{R}}\n" * 4,
    "bib/references.bib": "@book{knuth1984, title = {The TeXbook}, year = {1984}}\n" * 4,
}


def workflow_diff_args() -> list[str]:
    """Argumentos de git del ``git diff`` de modo pr de compile.yml (sin la redirección)."""
    commands = [
        m.group(0).strip()
        for step in steps_of(jobs("compile.yml")["compile"])
        for m in DIFF_RE.finditer(str(step.get("run", "")))
    ]
    assert len(commands) == 1, commands
    argv = shlex.split(commands[0])
    assert argv[:2] == ["git", "diff"] and "--name-only" in argv
    assert argv[-2:] == ["HEAD^1", "HEAD"]
    return argv[1:]


def merge_pr(repo: GitRepo, change: Sequence[Sequence[str]]) -> None:
    """Base en ``main``, PR en ``task/1`` y merge commit como ``refs/pull/<n>/merge`` (primer padre = base)."""
    repo.commit("base", files=BASE_FILES)
    repo.git("checkout", "-q", "-b", "task/1")
    for args in change:
        repo.git(*args)
    repo.git("commit", "-q", "--no-verify", "-m", "pr")
    repo.checkout("main")
    repo.merge_no_ff("task/1", "merge del PR")


@pytest.mark.parametrize(
    ("change", "source"),
    [
        ([["mv", "sections/intro/intro.tex", "notes/intro.tex"]], "sections/intro/intro.tex"),
        ([["mv", "tex/notation.tex", "old/notation.tex"]], "tex/notation.tex"),
        ([["mv", "bib/references.bib", "data/references.bib"]], "bib/references.bib"),
        ([["rm", "-q", "tex/notation.tex"]], "tex/notation.tex"),
    ],
    ids=["rename-part", "rename-tex-notation", "rename-bib", "delete-tex"],
)
@pytest.mark.parametrize("renames", [None, "true", "copies"])
def test_moving_a_build_input_out_of_tex_or_bib_still_compiles(
    git_repo: GitRepo, change: Sequence[Sequence[str]], source: str, renames: str | None
) -> None:
    for args in change:
        if args[0] == "mv":
            (git_repo.path / args[2]).parent.mkdir(parents=True, exist_ok=True)
    merge_pr(git_repo, change)
    extra = ["-c", f"diff.renames={renames}"] if renames else []
    proc = git_repo._run(git_repo.path, [*extra, *workflow_diff_args()], True)
    changed = proc.stdout.decode("utf-8").splitlines()
    assert source in changed, changed  # la ruta de origen también aparece
    assert changes_need_build(changed)  # build-plan emite run=true
