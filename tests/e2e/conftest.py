# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Infraestructura de las pruebas de punta a punta (00-marco D-37, 08-forja REQ-FORGE-62).

Las pruebas de ``tests/e2e`` ejecutan el lanzador instalado como subproceso, sin shell y
desde un directorio fuera del árbol validado, igual que los workflows de la plantilla:

- El lanzador debe estar en una carpeta absoluta del ``PATH``: en local, el de ``.venv``
  (instalación editable); en CI, el de ``uv tool install`` sobre la rueda recién
  construida. Si el venv no está activado se busca también en el directorio de scripts
  del intérprete en curso. El directorio actual no cuenta. Sin lanzador, o con una
  versión distinta de la del árbol, las pruebas fallan (no se omiten).
- El entorno de cada subproceso es determinista: se quitan las variables ``GIT_*``,
  ``GITHUB_*``, ``RUNNER_*``, ``CI`` y las del prefijo propio que traiga el proceso, se
  aísla git y se fijan ``<PREFIJO>TESTING=1``, ``<PREFIJO>FORGE_ADAPTER=fake``,
  ``<PREFIJO>FORGE_FAKE_STATE`` (un JSON por prueba) y ``<PREFIJO>GITLEAKS`` (el doble de
  ``tests/fixtures/tools/fake_gitleaks.py``; el binario real solo lo usa la prueba que
  lo pide explícitamente).
- ``WORKFLOW_COMMANDS`` es la lista canónica de las líneas ``aucove …`` de los
  workflows de la plantilla (los de H2 y, desde H3, los del editor del artículo, el
  vencimiento y la base rota). ``PENDING_COMMANDS`` son las que llaman a un subcomando que
  todavía no existe (desde 5d, ninguna). ``test_workflow_drift`` exige que sea exactamente
  la del render, y los
  módulos ``test_template_*`` ejecutan esas mismas líneas: ``expand`` las convierte en argv
  con las variables y los arreglos de bash que fija cada prueba, sin pasar por una shell.
- ``ci`` (de sesión) es E1 sobre el render ``latex`` en cada idioma
  (``template_repo.build_template_ci``); ``seed_git_history`` lleva esa historia al
  ``FakeForge`` con los mismos sha, leyendo los árboles por un solo ``git cat-file --batch``.

El conftest raíz no bloquea la red en ``tests/e2e``: la compilación con Docker
necesita bajar la imagen fijada. Las demás pruebas de esta carpeta no usan la red.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import sysconfig
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, cast

import pytest

from aucove import _meta
from aucove.domain.taskbody import parse_task_body, render_task_body
from aucove.forge.base import NULL_SHA, ChangedFile, LabelEvent, RefActivity
from aucove.forge.fake import FakeForge
from aucove.i18n import LANGS
from aucove.ops.doctor import find_launcher as find_in_path
from tests.conftest import FIXTURES, GIT_OPTIONS, isolated_git_env

if TYPE_CHECKING:
    from tests.e2e.template_repo import TemplateCi

NAME = _meta.NAME
PREFIX = _meta.ENV_PREFIX
CI_LOGIN = "github-actions[bot]"  # identidad del GITHUB_TOKEN en CI (08 REQ-FORGE-43)
FAKE_GITLEAKS = FIXTURES / "tools" / "fake_gitleaks.py"
LAUNCHER_TIMEOUT_S = 300.0

# Fechas fijas de las siembras (las mismas de GIT_IDENTITY en tests/conftest.py).
T0 = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


# -- lista canónica de las líneas 'aucove …' de los workflows ------------------------


class WorkflowCommand(NamedTuple):
    workflow: str  # archivo de .github/workflows/
    job: str  # id del job
    line: str  # línea normalizada: continuaciones unidas y espacios colapsados


def _cmd(rest: str) -> str:
    return f"{NAME} {rest}"


# En el orden de los archivos (ordenados), los jobs y los pasos. Fuente: 09-ci §3.2, §3.3,
# §4.3, §4.5, §6, §7.1 y §7.4, copiados en template/base/.github/workflows/.
WORKFLOW_COMMANDS: tuple[WorkflowCommand, ...] = (
    WorkflowCommand(
        "build.yml",
        "report",
        _cmd(
            'ci comment --kind build --pr "$PR_NUMBER" --language "$LANG_CODE" --outcome "$OUTCOME"'
            ' --pdf-url "$PDF_URL" --diff-url "$DIFF_URL" --skip-reason "$SKIP_REASON" --run-url "$RUN_URL"'
            ' --report-dir "$RUNNER_TEMP/reports" --summary "$GITHUB_STEP_SUMMARY"'
        ),
    ),
    WorkflowCommand(
        "compile.yml",
        "compile",
        _cmd(
            'ci build-plan --mode "$MODE" --policy "$RUNNER_TEMP/policy"'
            ' --changed "$RUNNER_TEMP/changed.txt" >> "$GITHUB_OUTPUT"'
        ),
    ),
    WorkflowCommand(
        "compile.yml",
        "compile",
        _cmd(
            'validate --mode build-log --log "tex/build/${MAIN_BASE}.log"'
            ' --report "$RUNNER_TEMP/out/diagnostics.json" --format github || true'
        ),
    ),
    WorkflowCommand(
        "editor-review.yml",
        "evaluate",
        _cmd('ci editor-review --pr "$PR_NUMBER" --policy "$GITHUB_WORKSPACE/base" --summary "$GITHUB_STEP_SUMMARY"'),
    ),
    WorkflowCommand("expire-claims.yml", "expire", _cmd('expire-claims --json > "$RUNNER_TEMP/expire.json"')),
    WorkflowCommand(
        "expire-claims.yml",
        "expire",
        _cmd(
            'ci comment --kind expire-claims --policy . --report "$RUNNER_TEMP/expire.json"'
            ' --summary "$GITHUB_STEP_SUMMARY"'
        ),
    ),
    WorkflowCommand(
        "main-broken.yml",
        "validate",
        _cmd('validate --mode base --policy . --tree . --report "$RUNNER_TEMP/base-validate.json" --format github'),
    ),
    WorkflowCommand(
        "main-broken.yml",
        "audit",
        _cmd('ci audit --before "$BEFORE" --after "$GITHUB_SHA" --forced "$FORCED"'),
    ),
    WorkflowCommand(
        "main-broken.yml",
        "report",
        _cmd(
            'ci base-report --policy . --sha "$GITHUB_SHA" --build "$BUILD" --build-reason "$BUILD_REASON"'
            ' --validate "$VALIDATE"'
            ' --pdf-url "$PDF_URL" --run-url "$RUN_URL" --reports "$RUNNER_TEMP/reports"'
            ' --summary "$GITHUB_STEP_SUMMARY"'
        ),
    ),
    WorkflowCommand(
        "seal.yml",
        "submission",
        _cmd(
            'ci seal --kind submission --policy "$GITHUB_WORKSPACE/base" --repo-dir "$GITHUB_WORKSPACE/pr"'
            ' --commit "$HEAD_SHA" --base-sha "$BASE_SHA" --pr "$PR_NUMBER" --out "$RUNNER_TEMP/evidence"'
            ' --summary "$GITHUB_STEP_SUMMARY"'
        ),
    ),
    WorkflowCommand(
        "seal.yml",
        "integration",
        _cmd(
            'ci seal --kind integration --policy "$GITHUB_WORKSPACE" --repo-dir "$GITHUB_WORKSPACE"'
            ' --commit "$GITHUB_SHA" --out "$RUNNER_TEMP/evidence" --summary "$GITHUB_STEP_SUMMARY"'
        ),
    ),
    WorkflowCommand(
        "validate.yml",
        "pin",
        _cmd('ci pin-plan --policy "$GITHUB_WORKSPACE/base" "${TREE[@]}" >> "$GITHUB_OUTPUT"'),
    ),
    WorkflowCommand(
        "validate.yml",
        "pin",
        _cmd(
            "validate --mode tree --check generated-sync --check roles-sync --check schemas"
            " --tree pr --report pin.json || true"
        ),
    ),
    WorkflowCommand(
        "validate.yml",
        "validate",
        _cmd(
            'validate --mode pr --pr "$PR_NUMBER" --policy "$GITHUB_WORKSPACE/base"'
            ' "${TREE[@]}" "${PINREP[@]}" --report "$RUNNER_TEMP/validate.json" --format github'
        ),
    ),
    WorkflowCommand(
        "validate.yml",
        "validate",
        _cmd(
            'ci comment --kind validate --pr "$PR_NUMBER" --policy "$GITHUB_WORKSPACE/base"'
            ' --report "$RUNNER_TEMP/validate.json" --summary "$GITHUB_STEP_SUMMARY"'
        ),
    ),
)

# Líneas de los workflows cuyo subcomando todavía no existe: el job falla con «no disponible»
# (cli/ci.py) hasta que llegue, y la autoprueba no las puede ejercer. Prefijo -> paso que las trae.
# Vacío desde 5d (ci audit): todas las líneas de H3 tienen su subcomando.
PENDING_COMMANDS: Mapping[str, str] = {}

# Salidas que escriben en $GITHUB_OUTPUT los subcomandos 'ci', en orden.
CI_COMMAND_OUTPUTS: Mapping[str, tuple[str, ...]] = {
    "build-plan": (
        "run", "image", "engine_flag", "shell", "timeout", "main", "main_base",
        "sha7", "retention", "latexdiff", "language", "reason",
    ),
    "pin-plan": ("run", "pin"),
}

# Reglas que corre test_a9 si el caso no declara 'checks'.
DEFAULT_A9_CHECKS: tuple[str, ...] = ("scope", "roles-sync", "generated-sync", "decisions-immutable")
PRIOR_RULES: tuple[str, ...] = ("roles-sync", "generated-sync")
PIN_RULES: tuple[str, ...] = ("schemas", "roles-sync", "generated-sync")


def workflow_command(prefix: str, *, commands: Sequence[WorkflowCommand] = WORKFLOW_COMMANDS) -> WorkflowCommand:
    """La única entrada de ``commands`` cuya línea empieza con ``<nombre> <prefix>``."""
    wanted = _cmd(prefix)
    found = [c for c in commands if c.line == wanted or c.line.startswith(wanted + " ")]
    if len(found) != 1:
        raise LookupError(f"{wanted!r} coincide con {len(found)} líneas de los workflows")
    return found[0]


# -- extracción de las líneas de un workflow -----------------------------------------

_CONTINUATION_RE = re.compile(r"\\\r?\n")


def command_lines(script: str) -> list[str]:
    """Líneas de ``script`` que invocan el lanzador, con continuaciones unidas y espacios colapsados.

    Cuenta toda línea en la que el nombre aparece como palabra suelta (no dentro de
    ``"aucove==${PIN}"``): así una invocación tras ``&&`` o ``;`` también se detecta.
    """
    out: list[str] = []
    for raw in _CONTINUATION_RE.sub(" ", script).splitlines():
        line = " ".join(raw.split())
        words = re.split(r"[\s;&|()]+", line)
        if NAME in words:
            out.append(line)
    return out


def workflow_jobs(doc: Mapping[Any, Any]) -> Mapping[str, Mapping[str, Any]]:
    jobs = doc.get("jobs")
    if not isinstance(jobs, Mapping):
        raise AssertionError("workflow sin jobs")
    return jobs


def extract_commands(workflows: Mapping[str, Mapping[Any, Any]]) -> list[WorkflowCommand]:
    """``WorkflowCommand`` de cada ``run:`` de los workflows ya cargados, en orden."""
    out: list[WorkflowCommand] = []
    for name in sorted(workflows):
        for job_id, job in workflow_jobs(workflows[name]).items():
            for step in job.get("steps") or []:
                run = step.get("run") if isinstance(step, Mapping) else None
                if isinstance(run, str):
                    out.extend(WorkflowCommand(name, str(job_id), line) for line in command_lines(run))
    return out


# -- de una línea del workflow a argv --------------------------------------------------

_ARRAY_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\[@\]\}")
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


@dataclass(frozen=True)
class Expanded:
    argv: tuple[str, ...]  # argv[0] es el nombre del lanzador
    append_to: str | None  # destino de '>> ARCHIVO' (p. ej. $GITHUB_OUTPUT)
    ignore_failure: bool  # la línea termina en '|| true'
    write_to: str | None = None  # destino de '> ARCHIVO' (se reemplaza)


def _expand_word(word: str, variables: Mapping[str, str]) -> str:
    def value(m: re.Match[str]) -> str:
        key = m.group(1) or m.group(2)
        if key not in variables:
            raise KeyError(f"la línea usa ${key}, que la prueba no definió")
        return variables[key]

    expanded = _VAR_RE.sub(value, word)
    if "$" in expanded.replace("$$", ""):
        raise ValueError(f"expansión no admitida en {word!r}")
    return expanded


def expand(
    line: str,
    variables: Mapping[str, str],
    arrays: Mapping[str, Sequence[str]] | None = None,
) -> Expanded:
    """argv de una línea del workflow, como la expandiría bash con esas variables.

    Admite lo que usan los workflows: palabras entre comillas, ``$VAR`` y ``${VAR}``,
    ``"${ARR[@]}"`` (que se expande a los elementos del arreglo), ``>> ARCHIVO`` o ``> ARCHIVO``
    y ``|| true`` al final. Cualquier otra cosa es un error: la prueba no debe adivinar.
    """
    tokens = shlex.split(line, posix=True)
    ignore = False
    if tokens[-2:] == ["||", "true"]:
        ignore = True
        tokens = tokens[:-2]
    append_to: str | None = None
    write_to: str | None = None
    if len(tokens) >= 2 and tokens[-2] == ">>":
        append_to = _expand_word(tokens[-1], variables)
        tokens = tokens[:-2]
    elif len(tokens) >= 2 and tokens[-2] == ">":
        write_to = _expand_word(tokens[-1], variables)
        tokens = tokens[:-2]
    argv: list[str] = []
    for token in tokens:
        if token in ("|", "||", "&&", ";", ">", ">>", "<", "&"):
            raise ValueError(f"operador de shell no admitido en {line!r}")
        m = _ARRAY_RE.fullmatch(token)
        if m:
            if arrays is None or m.group(1) not in arrays:
                raise KeyError(f"la línea usa el arreglo {m.group(1)}, que la prueba no definió")
            argv.extend(arrays[m.group(1)])
            continue
        argv.append(_expand_word(token, variables))
    if not argv or argv[0] != NAME:
        raise ValueError(f"la línea no invoca el lanzador: {line!r}")
    return Expanded(tuple(argv), append_to, ignore, write_to)


# -- lanzador y entorno ------------------------------------------------------------------


def find_launcher() -> str | None:
    """Ruta del lanzador: ``PATH`` y, si no está, el directorio de scripts del intérprete.

    Busca con ``doctor.find_launcher`` (01 REQ-ARCH-33), que solo mira carpetas absolutas:
    ``shutil.which`` de Python 3.11 en Windows antepone el directorio actual, y un
    ``aucove.exe`` que estuviera ahí reemplazaría al lanzador instalado.
    """
    found = find_in_path(NAME)
    if found is None:
        scripts = sysconfig.get_path("scripts")
        if scripts and Path(scripts).is_dir():
            found = find_in_path(NAME, path=scripts)
    return found


# Variables del proceso de pytest que no deben llegar al lanzador (las GIT_* ya las
# reemplaza isolated_git_env). En CI, GITHUB_SHA o CI=true cambiarían el resultado.
_DROPPED_PREFIXES: tuple[str, ...] = ("GITHUB_", "RUNNER_", PREFIX)
_DROPPED_NAMES = frozenset({"CI", "CLAUDE_CODE_CHILD_SESSION", "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"})


def base_env(tmp: Path) -> dict[str, str]:
    """Entorno determinista para el lanzador: git aislado y el adaptador falso de la forja."""
    env = {
        k: v
        for k, v in isolated_git_env(tmp / "git-env").items()
        if not (k.upper().startswith(_DROPPED_PREFIXES) or k.upper() in _DROPPED_NAMES)
    }
    env.update(
        {
            PREFIX + "TESTING": "1",
            PREFIX + "FORGE_ADAPTER": "fake",
            PREFIX + "FORGE_FAKE_STATE": str(tmp / "fake-state.json"),
            PREFIX + "GITLEAKS": json.dumps([sys.executable, str(FAKE_GITLEAKS)]),
            "PYTHONUTF8": "1",
        }
    )
    return env


def without(env: Mapping[str, str], *names: str) -> dict[str, str]:
    return {k: v for k, v in env.items() if k not in names}


@dataclass(frozen=True)
class Run:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    def explain(self) -> str:
        return f"$ {' '.join(self.args)}\n-> {self.returncode}\nstdout:\n{self.stdout}\nstderr:\n{self.stderr}"


class Launcher:
    """Ejecuta el lanzador instalado sin shell (``subprocess`` con argv)."""

    def __init__(self, path: str) -> None:
        self.path = path

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float = LAUNCHER_TIMEOUT_S,
    ) -> Run:
        argv = [str(a) for a in args]
        if argv and argv[0] == NAME:
            argv = argv[1:]
        proc = subprocess.run(
            [self.path, *argv],
            cwd=cwd,
            env=dict(env),
            capture_output=True,
            timeout=timeout_s,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        return Run(
            (NAME, *argv),
            proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"),
        )

    def run_line(
        self,
        command: WorkflowCommand | str,
        variables: Mapping[str, str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        arrays: Mapping[str, Sequence[str]] | None = None,
    ) -> Run:
        """Ejecuta una línea del workflow; ``>> ARCHIVO`` agrega stdout a ese archivo y ``> ARCHIVO`` lo reemplaza."""
        line = command.line if isinstance(command, WorkflowCommand) else command
        ex = expand(line, variables, arrays)
        result = self.run(ex.argv, cwd=cwd, env=env)
        for dest, mode in ((ex.append_to, "ab"), (ex.write_to, "wb")):
            if dest is None:
                continue
            target = Path(dest)
            if not target.is_absolute():
                target = cwd / target
            with target.open(mode) as fh:
                fh.write(result.stdout.encode("utf-8"))
        return result


@pytest.fixture(scope="session")
def launcher() -> Launcher:
    """El lanzador instalado; falla (no omite) si falta o si su versión no es la del árbol."""
    path = find_launcher()
    if path is None:
        pytest.fail(
            f"no se encuentra el lanzador '{NAME}' en el PATH: instala el paquete en el venv "
            f"('uv sync' o 'pip install -e .') o, en CI, con 'uv tool install' sobre la rueda",
            pytrace=False,
        )
    proc = subprocess.run([path, "--version"], capture_output=True, timeout=60, check=False)
    version = proc.stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0 or version != _meta.__version__:
        pytest.fail(
            f"el lanzador {path} informa {version!r} (salida {proc.returncode}); "
            f"se esperaba {_meta.__version__!r}, la versión del árbol",
            pytrace=False,
        )
    return Launcher(path)


@pytest.fixture
def e2e_env(tmp_path: Path) -> dict[str, str]:
    return base_env(tmp_path)


@pytest.fixture(scope="session", params=LANGS)
def ci(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> TemplateCi:
    """E1 sobre el render ``latex`` en cada idioma (``template_repo.build_template_ci``).

    Lo comparten ``test_template_ci`` y ``test_template_seal``: el render y los repositorios se
    arman una vez por proceso. Nadie lo modifica; quien necesita cambiar el remoto o el estado
    del fake trabaja sobre una copia.
    """
    from tests.e2e.template_repo import build_template_ci  # template_repo importa este módulo

    return build_template_ci(str(request.param), tmp_path_factory.mktemp(f"template-{request.param}"))


def parse_outputs(text: str) -> list[tuple[str, str]]:
    """Pares ``clave=valor`` de lo que un paso escribió en ``$GITHUB_OUTPUT``."""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line:
            continue
        key, sep, value = line.partition("=")
        assert sep, f"línea de salida sin '=': {line!r}"
        out.append((key, value))
    return out


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


# -- git ---------------------------------------------------------------------------------


def git(cwd: Path, env: Mapping[str, str], *args: str, check: bool = True, input: bytes | None = None) -> str:
    """git con las opciones aisladas de las pruebas (nunca 'git config'); devuelve stdout sin el salto final."""
    if args and args[0] == "config":
        raise AssertionError("las pruebas no ejecutan git config")
    proc = subprocess.run(
        ["git", *GIT_OPTIONS, *args],
        cwd=cwd,
        env=dict(env),
        capture_output=True,
        input=input,
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} -> {proc.returncode}: {proc.stderr.decode('utf-8', 'replace')}"
        )
    return proc.stdout.decode("utf-8", errors="replace").rstrip("\n")


class GitObjects:
    """Objetos de un repositorio leídos por un único ``git cat-file --batch`` interactivo.

    Cada proceso de git cuesta en Windows del orden de 0,1 s: leer los árboles de todos los
    commits de una siembra por la misma tubería evita un ``ls-tree`` y un ``cat-file`` por commit.
    """

    def __init__(self, repo: Path, env: Mapping[str, str]) -> None:
        self._proc = subprocess.Popen(
            ["git", *GIT_OPTIONS, "cat-file", "--batch"],
            cwd=repo, env=dict(env), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self._trees: dict[str, dict[str, bytes]] = {}

    def __enter__(self) -> GitObjects:
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.close()
        self._proc.wait(timeout=60)

    def read(self, oid: str) -> tuple[str, bytes]:
        """``(tipo, contenido)`` del objeto ``oid``."""
        assert self._proc.stdin is not None and self._proc.stdout is not None
        self._proc.stdin.write(oid.encode("ascii") + b"\n")
        self._proc.stdin.flush()
        header = self._proc.stdout.readline().split()
        if len(header) != 3:
            raise AssertionError(f"git cat-file no da el objeto {oid}: {header!r}")
        data = self._proc.stdout.read(int(header[2]))
        self._proc.stdout.read(1)  # el salto de línea tras el contenido
        return header[1].decode("ascii"), data

    def tree_files(self, tree: str) -> dict[str, bytes]:
        """Ruta -> bytes de los blobs del árbol ``tree``, recursivo (como ``ls-tree -r``, sin gitlinks)."""
        cached = self._trees.get(tree)
        if cached is not None:
            return cached
        kind, data = self.read(tree)
        assert kind == "tree", (tree, kind)
        width = len(tree) // 2  # bytes de cada id en el árbol (20 con SHA-1)
        out: dict[str, bytes] = {}
        pos = 0
        while pos < len(data):
            space = data.index(b" ", pos)
            nul = data.index(b"\0", space)
            mode, name = data[pos:space], data[space + 1:nul].decode("utf-8")
            oid = data[nul + 1:nul + 1 + width].hex()
            pos = nul + 1 + width
            if mode == b"40000":
                out.update({f"{name}/{path}": blob for path, blob in self.tree_files(oid).items()})
            elif mode != b"160000":  # blobs y enlaces simbólicos; un gitlink no es un blob
                out[name] = self.read(oid)[1]
        self._trees[tree] = out
        return out

    def commit_files(self, sha: str) -> dict[str, bytes]:
        kind, data = self.read(sha)
        first = data.split(b"\n", 1)[0]
        assert kind == "commit" and first.startswith(b"tree "), (sha, kind)
        return dict(self.tree_files(first[5:].decode("ascii")))


def commit_files(repo: Path, env: Mapping[str, str], sha: str) -> dict[str, bytes]:
    """Ruta -> bytes del árbol del commit ``sha`` (solo blobs)."""
    with GitObjects(repo, env) as objects:
        return objects.commit_files(sha)


def seed_git_history(forge: FakeForge, repo: Path, env: Mapping[str, str], revs: Iterable[str] = ("--all",)) -> list[str]:
    """Siembra en ``forge`` los commits de ``repo`` con los mismos sha, padres primero."""
    fmt = "%H%x00%P%x00%an%x00%aI%x00%B%x00%x01"
    raw = git(repo, env, "log", "--reverse", "--topo-order", f"--format={fmt}", *revs)
    shas: list[str] = []
    with GitObjects(repo, env) as objects:
        for record in raw.split("\x01"):
            record = record.lstrip("\n")
            if not record:
                continue
            sha, parents, author, date, message, _rest = record.split("\x00")
            forge.seed_commit(
                objects.commit_files(sha),
                parents.split(),
                message.rstrip("\n"),
                author,
                sha=sha,
                date=date,
            )
            shas.append(sha)
    return shas


def claim_marker(data: Mapping[str, object]) -> str:
    """Marcador ``claim`` de 04 §1.4 (sin ``--`` dentro del JSON, REQ-TASK-11)."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("--", "\\u002d\\u002d")
    return f"<!-- {NAME}:claim v1\n{payload}\n-->"


def seed_claimed_task(
    forge: FakeForge,
    *,
    number: int,
    lead: str,
    claimant: str,
    sections: Sequence[str],
    language: str,
    claim_sha: str,
    base_sha: str,
    head_sha: str,
    summary: str,
) -> None:
    """Issue de tarea aceptado por el ``lead``, marcador de reclamo y actividad de ``task/<n>`` (04 §1)."""
    spec = {"summary": summary, "sections": list(sections), "plan_refs": [], "done": ["CI PDF compiles."]}
    body = render_task_body(spec, language)
    spec_hash = str(parse_task_body(body)["spec_hash"])
    forge.seed_issue(
        number=number,
        title=f"Task {number}",
        body=body,
        state="open",
        labels=["task"],
        assignees=[claimant],
        author=lead,
        created_at=at(60),
        updated_at=at(120),
        events=[
            LabelEvent(1, "labeled", "task", lead, at(60)),
            LabelEvent(2, "labeled", "state:proposed", lead, at(60)),
            LabelEvent(3, "unlabeled", "state:proposed", lead, at(120)),  # aceptación (REQ-TASK-8)
        ],
    )
    marker = claim_marker(
        {
            "task": number,
            "login": claimant,
            "claim_sha": claim_sha,
            "base_sha": base_sha,
            "sections": list(sections),
            "spec_hash": spec_hash,
            "claimed_at": at(180).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "renewed_at": None,
        }
    )
    forge.seed_comment(number, author=claimant, body=marker, created_at=at(181))
    forge.seed_ref_activity(
        f"task/{number}",
        [
            RefActivity("branch_creation", claimant, at(180), NULL_SHA, claim_sha),
            RefActivity("push", claimant, at(241), claim_sha, head_sha),
        ],
    )


ApiStatus = Literal["added", "modified", "removed", "renamed", "copied", "changed", "unchanged"]


def seed_pull(
    forge: FakeForge,
    *,
    number: int,
    author: str,
    head_branch: str,
    head_sha: str,
    files: Sequence[tuple[str, ...]],
    base_branch: str = "main",
    body: str = "",
) -> None:
    """PR abierto; ``files`` son ``(estado de la API, ruta)`` o, en un renombre, ``(…, ruta anterior)``."""
    forge.seed_pull_request(
        body=body,
        files=[ChangedFile(f[1], cast(ApiStatus, f[0]), f[2] if len(f) > 2 else None) for f in files],
        number=number,
        title=f"PR {number}",
        head_branch=head_branch,
        head_sha=head_sha,
        base_branch=base_branch,
        author=author,
        draft=False,
        created_at=at(300),
    )


def comments_with_marker(state: Path, number: int, kind: str) -> list[str]:
    """Cuerpos de los comentarios del PR ``number`` que llevan el marcador de CI ``kind``."""
    forge = FakeForge.load(state)
    marker = _meta.ci_marker(kind)
    return [c.body for c in forge.list_comments(number) if marker in c.body]
