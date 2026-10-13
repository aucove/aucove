# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Los pasos del host de ``compile.yml`` no leen ni suben salidas del PR.

``compile.yml`` monta el checkout con escritura en el contenedor de TeX (09-ci REQ-CI-13) y
deja las salidas en ``tex/build/`` (REQ-CI-14). Después, en el host y fuera del sandbox, el
paso «Diagnóstico estructurado» lee ``tex/build/<main>.log`` y copia ``tex/build/<main>.pdf``
a ``$RUNNER_TEMP/out``, y «Subir PDF» lo publica. Antes nada vaciaba ``tex/build``:
``mkdir -p`` dejaba lo que trajera el PR, y ``[ -f ]`` y ``cp`` siguen enlaces simbólicos.

- Un PR que versionaba ``tex/build/main.pdf`` (o ``tex/build`` entero) como enlace a un
  archivo del runner lo subía como «PDF», y ``validate --mode build-log`` leía el log a
  través del enlace (09-ci §3.1, «Leer archivos del runner»).
- Un PR que versionaba un PDF normal lo publicaba como vista previa aunque la compilación
  no escribiera ninguno (10-seguridad TH-09, falsear el PDF).
- latexdiff ejecutaba ``git worktree add`` en el host después de que el contenedor escribiera
  en el checkout, y el ``sh`` del contenedor redirige a ``tex/diff.tex``, que el PR podía
  versionar como enlace (por ejemplo, a ``../.git/description``).

La prueba ejecuta con bash, en orden y con sus ``if:``, los pasos ``run:`` del job
``compile`` sobre un merge commit real (``HEAD^1`` = base). ``docker``, ``curl``, ``uv`` y
``aucove`` son dobles en el ``PATH``; ``git`` es el real, envuelto para registrar cuándo
corre. El doble de ``docker run`` imita el contenedor: solo escribe en directorios reales
del checkout (un enlace que sale de ``/work`` no lleva a ningún lado dentro del contenedor).

Los enlaces que el PR versiona (modo 120000) son enlaces reales donde git los crea (Linux,
el runner de CI); con ``core.symlinks=false`` (Windows) git escribe un archivo con el texto
del destino, que tampoco puede subirse como PDF. Los enlaces a directorios que deja el
checkout se crean con ``os.symlink`` o, en Windows, como *junction* (bash los ve como
enlaces). Así ningún caso depende de la plataforma para fallar con el defecto.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import GitRepo
from tests.regression.test_template_workflow_steps import Evaluator, step_env
from tests.render.test_template_workflows import action_of, jobs, steps_of

SHA7 = "abc1234"
SECRET = b"secreto del runner: no es un PDF\n"
PR_PDF = b"%PDF-1.5 vista previa que trae el PR\n"
PR_LOG = b"log que trae el PR\n"
BUILT_PDF = b"%PDF-1.5 salida de la compilacion\n"
BUILT_LOG = b"log de la compilacion\n"
DIFF_PDF = b"%PDF-1.5 salida de latexdiff\n"
PDF_UPLOAD = f"pdf-{SHA7}.pdf"
DIFF_UPLOAD = f"diff-{SHA7}.pdf"

BASE_FILES = {
    "project.yaml": 'core_pin: "0.1.0"\n',
    "tex/main.tex": "\\documentclass{article}\n\\begin{document}\nHola.\n\\end{document}\n",
}
PR_TEX = "\\documentclass{article}\n\\begin{document}\nHola, mundo.\n\\end{document}\n"

# -- dobles en el PATH ---------------------------------------------------------------------

FAKE_DOCKER = r"""#!/usr/bin/env bash
# Doble de docker. «pull» no hace nada; «run» imita el contenedor sobre /work = el checkout.
echo "docker $1" >> "$FAKE_DIR/events"
[ "$1" = run ] || exit 0
cd "$GITHUB_WORKSPACE" || exit 90
# Dentro del contenedor solo existe /work: se escribe solo en directorios reales del checkout.
real_dir() { [ ! -L tex ] && [ -d "$1" ] && [ ! -L "$1" ]; }
case "$*" in
  *"latexdiff --flatten"*)
    [ -f "$RUNNER_TEMP/old/tex/main.tex" ] || exit 91         # el montaje /old (la base)
    printf '%%DIF LATEXDIFF\n' > tex/diff.tex || exit 92      # la redirección de sh -c
    [ -e tex/build-diff ] || mkdir tex/build-diff
    real_dir tex/build-diff || exit 93
    cp "$FAKE_DIR/diff.pdf" tex/build-diff/diff.pdf ;;
  *)
    case "$FAKE_TEX" in
      fail) exit 12 ;;                                          # falla antes de escribir nada
      pdf)
        real_dir tex/build || exit 13
        cp "$FAKE_DIR/built.log" tex/build/main.log
        cp "$FAKE_DIR/built.pdf" tex/build/main.pdf ;;
      link)                                                     # defensa en profundidad
        rm -rf tex/build
        "$FAKE_PY" "$FAKE_DIR/mklink.py" "$FAKE_OUTSIDE" tex/build
        exit 12 ;;
    esac ;;
esac
"""

FAKE_AUCOVE = r"""#!/usr/bin/env bash
# Doble del núcleo: build-plan imprime el plan; build-log registra lo que lee como log.
case " $* " in
  *" ci build-plan "*) cat "$FAKE_DIR/plan.txt" ;;
  *" --mode build-log "*)
    log=; report=
    while [ $# -gt 0 ]; do
      case "$1" in --log) log=$2; shift ;; --report) report=$2; shift ;; esac
      shift
    done
    if [ -e "$log" ]; then cat -- "$log" >> "$FAKE_DIR/log-read"; fi
    printf '{}\n' > "$report" ;;
  *) echo "aucove (doble): orden no prevista: $*" >&2; exit 2 ;;
esac
"""

FAKE_GIT = r"""#!/usr/bin/env bash
echo "git $*" >> "$FAKE_DIR/events"
exec "$FAKE_REAL_GIT" "$@"
"""

FAKE_CURL = r"""#!/usr/bin/env bash
while [ $# -gt 0 ]; do
  if [ "$1" = -o ]; then : > "$2"; shift; fi
  shift
done
"""

FAKE_UV = "#!/usr/bin/env bash\nexit 0\n"

MKLINK = """import os
import sys

target, link = sys.argv[1], sys.argv[2]
if os.name == "nt":
    import _winapi

    _winapi.CreateJunction(os.path.abspath(target), os.path.abspath(link))
else:
    os.symlink(target, link, target_is_directory=True)
"""


def make_dir_link(target: Path, link: Path) -> None:
    """Enlace a un directorio: simbólico en POSIX y *junction* en Windows (sin privilegios)."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target.resolve()), str(link.absolute()))
    else:
        os.symlink(target, link, target_is_directory=True)


def find_bash() -> str:
    """bash de verdad: en Windows, la de Git for Windows (nunca la de WSL de System32)."""
    if os.name != "nt":
        found = shutil.which("bash")
        assert found, "los pasos de compile.yml se ejecutan con bash"
        return found
    git = shutil.which("git")
    assert git, "se necesita git"
    for parent in Path(git).resolve().parents:
        candidate = parent / "usr" / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate)
    raise AssertionError("no se encontró la bash de Git for Windows")


# -- ejecución del job ---------------------------------------------------------------------


@dataclass
class Runner:
    workspace: Path
    temp: Path  # $RUNNER_TEMP
    fakes: Path  # dobles y registros
    outside: Path  # archivos del runner fuera del checkout
    bash: str
    env: dict[str, str]


@dataclass
class JobResult:
    failed: bool
    steps: dict[str, dict[str, Any]]
    uploads: dict[str, bytes]
    events: list[str]
    log_read: bytes
    stderr: list[str]

    def outcome(self, step_id: str) -> str | None:
        return self.steps.get(step_id, {}).get("outcome")

    def explain(self) -> str:
        return "\n".join([*self.events, *self.stderr])


@pytest.fixture
def runner(git_repo: GitRepo, tmp_path: Path) -> Runner:
    fakes = tmp_path / "fakes"
    temp = tmp_path / "runner-temp"
    outside = tmp_path / "runner-home"
    for directory in (fakes, temp, outside):
        directory.mkdir()
    (outside / "main.pdf").write_bytes(SECRET)
    (outside / "main.log").write_bytes(SECRET)
    (outside / "main.tex").write_bytes(BASE_FILES["tex/main.tex"].encode())
    (outside / "build").mkdir()
    (outside / "build" / "main.pdf").write_bytes(SECRET)
    (outside / "build" / "main.log").write_bytes(SECRET)
    scripts = {"docker": FAKE_DOCKER, "aucove": FAKE_AUCOVE, "git": FAKE_GIT,
               "curl": FAKE_CURL, "uv": FAKE_UV}
    for name, text in scripts.items():
        path = fakes / name
        path.write_bytes(text.encode("utf-8"))
        path.chmod(0o755)
    (fakes / "mklink.py").write_bytes(MKLINK.encode("utf-8"))
    (fakes / "built.pdf").write_bytes(BUILT_PDF)
    (fakes / "built.log").write_bytes(BUILT_LOG)
    (fakes / "diff.pdf").write_bytes(DIFF_PDF)
    (fakes / "events").write_bytes(b"")
    (fakes / "log-read").write_bytes(b"")
    bash = find_bash()
    real_git = shutil.which("git")
    assert real_git
    env = dict(git_repo.env)
    env.update({
        "PATH": os.pathsep.join([str(fakes), str(Path(bash).parent), env.get("PATH", "")]),
        "FAKE_DIR": fakes.as_posix(),
        "FAKE_REAL_GIT": Path(real_git).as_posix(),
        "FAKE_PY": Path(sys.executable).as_posix(),
        "FAKE_OUTSIDE": outside.as_posix(),
        "FAKE_TEX": "fail",
        "GITHUB_WORKSPACE": git_repo.path.as_posix(),
        "RUNNER_TEMP": temp.as_posix(),
    })
    return Runner(git_repo.path, temp, fakes, outside, bash, env)


def prepare_pr(
    repo: GitRepo, files: Mapping[str, bytes] | None = None, links: Mapping[str, str] | None = None
) -> None:
    """Base en ``main``; el PR (``task/1``) versiona ``files`` y ``links`` (modo 120000); merge commit."""
    repo.commit("base", files=BASE_FILES)
    repo.git("checkout", "-q", "-b", "task/1")
    repo.write({"tex/main.tex": PR_TEX, **(files or {})})
    repo.git("add", "-A")
    for path, target in (links or {}).items():
        blob_src = repo.path.parent / "link-target.txt"
        blob_src.write_bytes(target.encode("utf-8"))
        blob = repo.git("hash-object", "-w", "--", str(blob_src))
        repo.git("update-index", "--add", "--cacheinfo", f"120000,{blob},{path}")
    repo.git("commit", "-q", "--no-verify", "-m", "pr")
    repo.git("checkout", "-q", "-f", "main")
    repo.merge_no_ff("task/1", "merge del PR")
    assert repo.git("rev-parse", "HEAD^2")  # el checkout es el merge commit, HEAD^1 = base


def plan_lines(*, latexdiff: bool) -> str:
    pairs = {
        "run": "true", "image": "registry.example/texlive@sha256:" + "0" * 64,
        "engine_flag": "-pdf", "shell": "-no-shell-escape", "timeout": "10",
        "main": "tex/main.tex", "main_base": "main", "sha7": SHA7, "retention": "14",
        "latexdiff": "true" if latexdiff else "false", "language": "es",
    }
    return "".join(f"{k}={v}\n" for k, v in pairs.items())


def run_compile_job(r: Runner, *, tex: str, latexdiff: bool = False, mode: str = "pr") -> JobResult:
    """Ejecuta los pasos del job ``compile`` como lo haría el runner (``bash -e`` por paso)."""
    (r.fakes / "plan.txt").write_bytes(plan_lines(latexdiff=latexdiff).encode())
    env = {**r.env, "FAKE_TEX": tex}
    steps_ctx: dict[str, dict[str, Any]] = {}
    context = {
        "inputs": {"mode": mode},
        "steps": steps_ctx,
        "runner": {"temp": r.temp.as_posix()},
        "github": {"workspace": r.workspace.as_posix()},
    }
    uploads: dict[str, bytes] = {}
    stderr: list[str] = []
    success = True
    for index, step in enumerate(steps_of(jobs("compile.yml")["compile"])):
        ev = Evaluator(context, success=success)
        step_id = str(step.get("id", f"__{index}"))
        if not ev.runs(step):
            steps_ctx[step_id] = {"outcome": "skipped", "conclusion": "skipped", "outputs": {}}
            continue
        outputs: dict[str, str] = {}
        action = action_of(step)
        if action in ("actions/checkout", "astral-sh/setup-uv"):
            ok = True  # el checkout ya es el merge commit; uv es un doble
        elif action == "actions/upload-artifact":
            path = Path(ev.interpolate((step.get("with") or {})["path"]))
            if path.is_file():  # sin archivo, upload-artifact solo avisa
                uploads[path.name] = path.read_bytes()
                outputs["artifact-url"] = f"https://example.invalid/artifacts/{len(uploads)}"
            ok = True
        else:
            assert "run" in step and action is None, step
            out_file = r.fakes / f"output-{index}"
            out_file.write_bytes(b"")
            script = r.fakes / f"step-{index}.sh"
            script.write_bytes(str(step["run"]).encode("utf-8"))
            workdir = ev.interpolate(step["working-directory"]) if "working-directory" in step else None
            proc = subprocess.run(
                [r.bash, "--noprofile", "--norc", "-e", script.as_posix()],
                cwd=workdir or r.workspace,
                env={**env, **step_env(ev, step), "GITHUB_OUTPUT": out_file.as_posix()},
                capture_output=True, timeout=120, check=False,
            )
            ok = proc.returncode == 0
            if not ok:
                detail = proc.stderr.decode("utf-8", "replace")
                stderr.append(f"paso {index} ({step.get('name')}): rc={proc.returncode} {detail}")
            for line in out_file.read_text(encoding="utf-8").splitlines():
                key, sep, value = line.partition("=")
                assert sep, f"salida no admitida: {line!r}"
                outputs[key] = value
        outcome = "success" if ok else "failure"
        conclusion = "success" if ok or step.get("continue-on-error") is True else "failure"
        steps_ctx[step_id] = {"outcome": outcome, "conclusion": conclusion, "outputs": outputs}
        if conclusion == "failure":
            success = False
    events = (r.fakes / "events").read_text(encoding="utf-8").splitlines()
    return JobResult(not success, steps_ctx, uploads, events, (r.fakes / "log-read").read_bytes(), stderr)


def assert_nothing_from_the_pr_or_the_runner(r: Runner, job: JobResult) -> None:
    for name, data in job.uploads.items():
        for forbidden in (SECRET, PR_PDF, PR_LOG, r.outside.as_posix().encode()):
            assert forbidden not in data, (name, data, job.explain())
    for forbidden in (SECRET, PR_LOG):
        assert forbidden not in job.log_read, job.explain()
    # rm -rf quitó los enlaces sin tocar su destino
    assert (r.outside / "main.pdf").read_bytes() == SECRET
    assert (r.outside / "build" / "main.pdf").read_bytes() == SECRET


# -- casos -----------------------------------------------------------------------------------


def test_pr_preview_pdf_is_never_uploaded_when_the_build_writes_none(
    runner: Runner, git_repo: GitRepo
) -> None:
    prepare_pr(git_repo, files={"tex/build/main.pdf": PR_PDF, "tex/build/main.log": PR_LOG})
    job = run_compile_job(runner, tex="fail")
    assert job.outcome("tex") == "failure" and not job.failed, job.explain()
    assert job.outcome("diag") == "success", job.explain()
    assert job.steps["diag"]["outputs"].get("has_pdf") != "true"
    assert PDF_UPLOAD not in job.uploads
    assert job.log_read == b""  # la compilación no escribió log: no se lee el del PR
    assert_nothing_from_the_pr_or_the_runner(runner, job)


def test_pr_symlinked_pdf_to_a_runner_file_is_not_uploaded(runner: Runner, git_repo: GitRepo) -> None:
    secret = (runner.outside / "main.pdf").as_posix()
    prepare_pr(git_repo, links={"tex/build/main.pdf": secret, "tex/build/main.log": secret})
    job = run_compile_job(runner, tex="fail")
    assert not job.failed, job.explain()
    assert PDF_UPLOAD not in job.uploads
    assert job.log_read == b""
    assert_nothing_from_the_pr_or_the_runner(runner, job)


def test_checkout_with_tex_build_linked_outside_reads_nothing_through_it(
    runner: Runner, git_repo: GitRepo
) -> None:
    prepare_pr(git_repo)
    make_dir_link(runner.outside, git_repo.path / "tex" / "build")
    job = run_compile_job(runner, tex="fail")
    assert not job.failed, job.explain()
    assert PDF_UPLOAD not in job.uploads
    assert job.log_read == b""
    assert_nothing_from_the_pr_or_the_runner(runner, job)
    assert not (git_repo.path / "tex" / "build").is_symlink()


def test_checkout_with_tex_linked_outside_fails_closed(runner: Runner, git_repo: GitRepo) -> None:
    prepare_pr(git_repo)
    shutil.rmtree(git_repo.path / "tex")
    make_dir_link(runner.outside, git_repo.path / "tex")
    job = run_compile_job(runner, tex="fail")
    assert job.failed  # aucove/build en rojo: no se compila ni se lee nada a través de tex/
    assert job.uploads == {}
    assert job.log_read == b""
    assert_nothing_from_the_pr_or_the_runner(runner, job)
    assert sorted(p.name for p in (runner.outside / "build").iterdir()) == ["main.log", "main.pdf"]


def test_links_left_after_the_sandbox_are_not_followed(runner: Runner, git_repo: GitRepo) -> None:
    """Defensa en profundidad: si tras el contenedor ``tex/build`` fuera un enlace, el host no lo sigue."""
    prepare_pr(git_repo)
    job = run_compile_job(runner, tex="link")
    assert PDF_UPLOAD not in job.uploads
    assert job.log_read == b""
    assert job.failed  # el diagnóstico falla cerrado: aucove/build en rojo
    assert_nothing_from_the_pr_or_the_runner(runner, job)


def test_successful_build_uploads_its_own_pdf(runner: Runner, git_repo: GitRepo) -> None:
    prepare_pr(git_repo, files={"tex/build/main.pdf": PR_PDF, "tex/build/main.log": PR_LOG})
    job = run_compile_job(runner, tex="pdf")
    assert not job.failed and job.outcome("tex") == "success", job.explain()
    assert job.steps["diag"]["outputs"].get("has_pdf") == "true"
    assert job.uploads.get(PDF_UPLOAD) == BUILT_PDF, (job.steps, job.explain())
    assert job.log_read == BUILT_LOG
    assert job.steps["pdf"]["outputs"].get("artifact-url")
    assert_nothing_from_the_pr_or_the_runner(runner, job)


def test_latexdiff_no_host_git_after_the_sandbox_and_no_pr_links(runner: Runner, git_repo: GitRepo) -> None:
    description = (git_repo.path / ".git" / "description").read_bytes()
    prepare_pr(git_repo, files={"tex/build-diff/diff.pdf": PR_PDF},
               links={"tex/diff.tex": "../.git/description"})
    job = run_compile_job(runner, tex="pdf", latexdiff=True)
    assert not job.failed, job.explain()
    assert job.outcome("diff") == "success", job.explain()
    assert job.uploads.get(DIFF_UPLOAD) == DIFF_PDF, (job.steps, job.explain())
    assert job.uploads.get(PDF_UPLOAD) == BUILT_PDF, (job.steps, job.explain())
    # el sh del contenedor no escribió a través de un enlace del PR
    assert (git_repo.path / ".git" / "description").read_bytes() == description
    # ningún git del host corre después de que el contenedor escribió en el checkout
    first_run = job.events.index("docker run")
    assert [e for e in job.events[first_run:] if e.startswith("git ")] == [], job.events
    assert any(e.startswith("git worktree add") for e in job.events[:first_run]), job.events
    assert_nothing_from_the_pr_or_the_runner(runner, job)
