# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Repositorio de la autoprueba: E1 sobre el render de la plantilla (00-marco D-37; 12-mvp E1 y E11).

``build_template_ci(lang, root, fmt=...)`` renderiza la plantilla con
``tools/render_template.render_to`` en la variante ``latex`` (la publicada) o ``none`` (un
proyecto sin manuscrito, REQ-ARCH-100) y arma un repositorio con el render como commit raíz
de ``main``, un remoto ``origin`` bare (``file://``) y PR del ``lead`` desde ramas de tarea.
Cada PR deja en el remoto, como GitHub, su commit de fusión en ``refs/pull/<n>/merge`` y su
cabeza en ``refs/pull/<n>/head``, que sigue ahí aunque se borre la rama:

- PR 21 (tarea 7, ``task/7``): cambia la parte ``introduction`` del ``lead``:
  ``sections/introduction/introduction.tex`` con ``latex`` e ``introduction.md`` con
  ``none`` (E11: las partes en Markdown).
- Solo con ``latex``, PR 22 (tarea 8, ``task/8``): sube ``core_pin`` a una versión distinta
  que satisface ``core``, para que el job ``pin`` de ``validate.yml`` corra (REQ-CI-45).
- Solo con ``latex``, PR 23 (tarea 9, ``task/9``): ``git mv tex/notation.tex
  old/notation.tex``. Con la detección de renombres de git, ``git diff --name-only`` solo
  lista el destino, que está fuera de ``tex/``; el paso 'Política de la base' de
  ``compile.yml`` usa ``--no-renames`` para que el origen también cuente y ``build-plan``
  compile (REQ-CI-8).

Cada rama queda como la dejan ``claim``, ``save`` y ``submit``: el commit vacío de reclamo con
``Task`` y ``AI-Use: none``, el del cambio con sus trailers y la nota de sesión
``memory/notes/task-<n>.md``, y un cuerpo de PR como el de ``submit`` (bloque ``pr-meta`` con
``trace`` y trailers con ``Signed-off-by``), para que la regla ``traceability`` pase (11 §2.10); el PR
lleva el comentario de entrega con la evaluación de traslape de su cabeza (regla ``assessment``).

Issues y PR comparten numeración en GitHub, así que las tareas y los PR llevan números
distintos. ``FakeForge`` lleva los mismos commits (mismos sha), las tareas reclamadas (Issue
aceptado, marcador y actividad de la rama) y los PR abiertos; cada prueba trabaja sobre una
copia de su estado (``TemplateCi.env``).

También están aquí los pasos que comparten los módulos de la autoprueba: los checkouts de
``actions/checkout``, el sobre de un reporte y los pasos 'Política de la base' y 'Plan de
compilación' de ``compile.yml`` (``build_plan``), que leen del render las opciones de git.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from aucove import _meta
from aucove.config.project import ProjectConfig, parse_project
from aucove.config.roles import parse_roles
from aucove.config.schemas import validate_result
from aucove.domain.trailers import commit_message, trailer_block
from aucove.forge.base import RepoRef
from aucove.forge.fake import FakeClock, FakeForge
from aucove.i18n import translator
from aucove.ops.prbody import render_body
from aucove.ops.result import read_envelope
from tests.conftest import isolated_git_env, load_tool
from tests.e2e.conftest import (
    CI_LOGIN,
    PREFIX,
    Launcher,
    Run,
    at,
    base_env,
    git,
    parse_outputs,
    seed_claimed_task,
    seed_git_history,
    seed_pull,
    workflow_command,
)

FORMATS: tuple[str, ...] = ("latex", "none")  # project.yaml: format (D-43)
# Archivo de la parte introduction en cada variante (02 §2.2; REQ-ARCH-100).
INTRO_BY_FORMAT: Mapping[str, str] = {
    "latex": "sections/introduction/introduction.tex",
    "none": "sections/introduction/introduction.md",
}
INTRO = INTRO_BY_FORMAT["latex"]
NOTATION = "tex/notation.tex"
MOVED_NOTATION = "old/notation.tex"  # fuera de tex/ y de bib/
EXIT_VALIDATION = 3

PR_CONTENT = 21  # tarea 7: parte propia
PR_PIN = 22  # tarea 8: core_pin distinto
PR_RENAME = 23  # tarea 9: tex/notation.tex movido fuera de tex/
FAKE_URL = "https://forge.invalid/example-org/aucove-template/actions/runs/1"

# Reglas de cada modo (09-ci §4.1).
PR_RULES = frozenset(
    {"scope", "schemas", "roles-sync", "generated-sync", "decisions-immutable", "plan-ids", "citations",
     "labels-refs", "latex-macros", "instructions-lint", "secrets", "traceability", "ci-policy", "assessment",
     "direction-ack",
     "convention"}
)
_PR_ONLY = frozenset({"scope", "secrets", "traceability", "assessment", "direction-ack"})
TREE_RULES = PR_RULES - _PR_ONLY - {"decisions-immutable"}
BASE_RULES = PR_RULES - _PR_ONLY

# Pasos de compile.yml que la autoprueba lee del render (por id, o por el comienzo del nombre
# si el paso no tiene id).
POLICY_STEP = "policy"  # id de 'Política de la base (nunca del PR)'
PLAN_STEP = "plan"  # id de 'Plan de compilación'
TEX_STEP = "tex"  # id de 'Compilar en sandbox'
EMPTY_OUTPUTS_STEP = "Salidas de TeX vacías"  # nombre; sin id, corre antes de compilar


# -- escenario ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Pr:
    number: int
    task: int
    branch: str
    claim_sha: str
    head_sha: str
    merge_sha: str
    files: tuple[tuple[str, ...], ...]  # (estado de la API, ruta[, ruta anterior de un renombre])

    @property
    def changed(self) -> list[str]:
        """Lo que lista ``git diff --name-only --no-renames``: ambos lados de un renombre, en orden."""
        return sorted({path for f in self.files for path in f[1:]})


@dataclass
class TemplateCi:
    lang: str
    root: Path
    origin: Path
    rendered: list[str]
    root_sha: str
    prs: Mapping[int, Pr]
    project: ProjectConfig
    project_bytes: bytes
    lead: str
    state0: Path
    git_env: dict[str, str] = field(default_factory=dict)
    fmt: str = "latex"

    @property
    def origin_url(self) -> str:
        return self.origin.as_uri()

    @property
    def intro(self) -> str:
        return INTRO_BY_FORMAT[self.fmt]

    def env(self, tmp: Path) -> dict[str, str]:
        """Entorno del lanzador con una copia propia del estado del fake."""
        env = base_env(tmp)
        state = Path(env[PREFIX + "FORGE_FAKE_STATE"])
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_bytes(self.state0.read_bytes())
        return env

    # Checkouts como los de actions/checkout (sin credenciales; origin es file://).

    def clone_main(self, dest: Path, *, depth: int | None = None) -> Path:
        args = ["clone", "-q", "--branch", "main"]
        if depth is not None:
            args += ["--depth", str(depth)]
        git(dest.parent, self.git_env, *args, self.origin_url, str(dest))
        return dest

    def checkout_head(self, dest: Path, pr: Pr) -> Path:
        """``pr/`` de validate.yml y de seal.yml: la cabeza del PR con fetch-depth 0 (todas las ramas)."""
        git(dest.parent, self.git_env, "clone", "-q", "--no-checkout", self.origin_url, str(dest))
        git(dest, self.git_env, "checkout", "-q", "--detach", pr.head_sha)
        return dest

    def checkout_merge(self, dest: Path, pr: Pr) -> Path:
        """Checkout de compile.yml y build.yml: ``refs/pull/<n>/merge`` con fetch-depth 2."""
        dest.mkdir(parents=True)
        git(dest, self.git_env, "init", "-q")
        git(dest, self.git_env, "remote", "add", "origin", self.origin_url)
        ref = f"refs/pull/{pr.number}/merge"
        git(dest, self.git_env, "fetch", "-q", "--depth", "2", "origin", f"+{ref}:refs/remotes/pull/{pr.number}/merge")
        git(dest, self.git_env, "checkout", "-q", "--detach", f"refs/remotes/pull/{pr.number}/merge")
        return dest

    def workspace(self, tmp: Path, pr: Pr | None, *, fork: bool = False) -> Path:
        """``$GITHUB_WORKSPACE`` de validate.yml: ``base/`` (rama por defecto) y ``pr/``."""
        ws = tmp / "workspace"
        ws.mkdir(parents=True)
        self.clone_main(ws / "base", depth=1)
        if pr is not None and not fork:
            self.checkout_head(ws / "pr", pr)
        return ws


def bump_core_pin(text: bytes, pin: str) -> bytes:
    out, n = re.subn(rb'^core_pin: "[0-9A-Za-z.]+"', b'core_pin: "' + pin.encode() + b'"', text, count=1, flags=re.M)
    assert n == 1, "el project.yaml de la plantilla no tiene la línea core_pin esperada"
    return out


def next_patch(version: str) -> str:
    """La versión final del parche siguiente; con una preliminar (00-marco D-66), la de su X.Y.Z + 1."""
    found = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:(?:a|b|rc)[0-9]+)?", version)
    assert found is not None, version
    major, minor, patch = (int(found.group(i)) for i in (1, 2, 3))
    return f"{major}.{minor}.{patch + 1}"


def note_text(task: int, lead: str) -> str:
    """La nota de sesión que deja ``submit`` sin uso de IA (03 §7; REQ-SCHEMA-86)."""
    return f"---\ntask: {task}\ncreated: 2026-09-29\ncreated_by: {lead}\nai_use: [none]\n---\n"


def submit_body(lang: str, *, task: int, sections: list[str], login: str, signer: tuple[str, str], base: str,
                head: str) -> str:
    """El cuerpo del PR que escribe ``submit`` sin uso de IA (03 §9.2; 11 §3.3)."""
    trace = {
        "version": 1, "task": task, "sections": sections, "ai_use": ["none"],
        "ai_use_by_section": {s: ["none"] for s in sections}, "assisted_by": [],
        "sign_off": {"login": login, "name": signer[0], "email": signer[1], "channel": "tty", "attestation": "attest-v1",
                     "at": "2026-09-29T12:00:00Z"},
        "commits": {"base": base, "head": head, "count": 2, "undeclared": 0},
        "core_version": _meta.__version__,
    }
    meta = {"schema": 1, "task": task, "sections": sections, "plan_refs": [], "decisions": [], "trace": trace}
    trailers = trailer_block(task=task, sections=sections, ai_use=["none"], assisted=()) + \
        f"Signed-off-by: {signer[0]} <{signer[1]}>\n"
    return render_body(lang=translator(lang), number=task, summary=f"Task {task} of the template self-test.",
                       replaces=None, parts={}, ai_use=["none"], assisted=[], ai_note=None, decisions=[],
                       hook_count=0, meta=meta, trailers=trailers)


def submission_comment(task: int, head: str, lead: str) -> str:
    """El comentario de entrega con la evaluación de la cabeza, sin traslape (04 §8.3, REQ-TASK-62)."""
    data = {"task": task, "phase": "submission", "head_sha": head, "verdict": "none", "effective_verdict": "none",
            "overlaps": [], "assessed_by": lead, "recorded_at": "2026-09-29T12:00:00Z"}
    return f"<!-- {_meta.NAME}:assessment v1\n{json.dumps(data, separators=(',', ':'))}\n-->"


def build_template_ci(lang: str, root: Path, fmt: str = "latex") -> TemplateCi:
    """Render ``fmt`` en ``lang``, repositorio, ``origin`` y ``FakeForge`` de E1 (ver el docstring del módulo)."""
    assert fmt in FORMATS, fmt
    render_template = load_tool("render_template")
    genv = isolated_git_env(root / "git-env")
    dev = root / "dev"
    origin = root / "origin.git"
    rendered = list(render_template.render_to(dev, lang, fmt=fmt))
    project_bytes = (dev / "project.yaml").read_bytes()
    cfg, problems = parse_project(project_bytes, file="project.yaml")
    assert cfg is not None, problems
    roles, problems = parse_roles((dev / "roles.yaml").read_bytes(), file="roles.yaml")
    assert roles is not None, problems
    lead = roles.lead
    owned = [s.id for s in roles.sections if s.owner == lead]
    assert "introduction" in owned
    person = roles.person(lead)
    assert person is not None
    signer = (person.name, f"1+{lead}@users.noreply.github.com")  # REQ-TRACE-24, sin email ni forge_id

    def g(*args: str, cwd: Path = dev) -> str:
        return git(cwd, genv, *args)

    def commit(message: str, *, allow_empty: bool = False) -> str:
        if not allow_empty:
            g("add", "-A")
        g("commit", "-q", "--no-verify", f"--author={lead} <{lead}@example.invalid>", "-m", message,
          *(["--allow-empty"] if allow_empty else []))
        return g("rev-parse", "HEAD")

    git(root, genv, "init", "-q", "--bare", "-b", "main", str(origin))
    g("init", "-q", "-b", "main")
    g("remote", "add", "origin", origin.as_uri())
    root_sha = commit("Initial commit from the template")
    pushed = ["main"]  # un solo push al final: cada proceso de git cuesta

    intro = INTRO_BY_FORMAT[fmt]
    contents = {
        intro: (dev / intro).read_bytes() + b"\nThis sentence was added by the task branch.\n",
        "project.yaml": bump_core_pin(project_bytes, next_patch(cfg.core_pin)),
    }
    changes: dict[int, tuple[int, tuple[tuple[str, ...], ...]]] = {PR_CONTENT: (7, (("modified", intro),))}
    if fmt == "latex":
        changes[PR_PIN] = (8, (("modified", "project.yaml"),))
        changes[PR_RENAME] = (9, (("renamed", MOVED_NOTATION, NOTATION),))  # como lo informa la API
    prs: dict[int, Pr] = {}
    bodies: dict[int, str] = {}
    for number, (task, files) in changes.items():
        branch = f"task/{task}"
        g("checkout", "-q", "-b", branch, "main")
        claim_sha = commit(f"{_meta.CLAIM_SUBJECT}{task}\n\nTask: #{task}\nAI-Use: none", allow_empty=True)
        for status, path, *previous in files:
            if status == "renamed":
                (dev / path).parent.mkdir(parents=True, exist_ok=True)
                g("mv", previous[0], path)
            else:
                (dev / path).write_bytes(contents[path])
        note = f"memory/notes/task-{task}.md"
        (dev / note).parent.mkdir(parents=True, exist_ok=True)
        (dev / note).write_text(note_text(task, lead), encoding="utf-8")
        files = (*files, ("added", note))
        sections = sorted({p.split("/")[1] for f in files for p in f[1:] if p.startswith("sections/")})
        head_sha = commit(commit_message(f"Task {task}", trailer_block(task=task, sections=sections, ai_use=["none"],
                                                                      assisted=())))
        bodies[number] = submit_body(lang, task=task, sections=sections, login=lead, signer=signer, base=root_sha,
                                     head=head_sha)
        g("checkout", "-q", "--detach", "main")
        g("merge", "-q", "--no-ff", "--no-edit", "-m", f"Merge {head_sha} into {root_sha}", branch)
        merge_sha = g("rev-parse", "HEAD")
        pushed += [branch, f"{merge_sha}:refs/pull/{number}/merge", f"{head_sha}:refs/pull/{number}/head"]
        prs[number] = Pr(number, task, branch, claim_sha, head_sha, merge_sha, files)
    g("checkout", "-q", "main")
    g("push", "-q", "origin", *pushed)

    owner, name = cfg.forge_repo.split("/", 1)
    forge = FakeForge(RepoRef(owner, name), viewer=CI_LOGIN, profile="enforced", clock=FakeClock(at(420)),
                      default_branch=cfg.base_branch)
    # El lead es colaborador con ``admin`` (06-permisos §4.4): así es asignable, como en GitHub,
    # donde una cuenta sin acceso ni participación se descarta en silencio de ``assignees``.
    forge.seed_user(lead, permission="admin")
    seed_git_history(forge, dev, genv)
    forge.seed_branch(cfg.base_branch, root_sha)
    for pr in prs.values():
        forge.seed_branch(pr.branch, pr.head_sha)
        seed_claimed_task(  # la del PR 23 no cubre tex/notation.tex: solo se usa en compile.yml
            forge, number=pr.task, lead=lead, claimant=lead, sections=["introduction"], language=lang,
            claim_sha=pr.claim_sha, base_sha=root_sha, head_sha=pr.head_sha,
            summary=f"Task {pr.task} of the template self-test.",
        )
        seed_pull(forge, number=pr.number, author=lead, head_branch=pr.branch, head_sha=pr.head_sha,
                  files=pr.files, base_branch=cfg.base_branch, body=bodies[pr.number])
        forge.seed_comment(pr.number, author=lead, body=submission_comment(pr.task, pr.head_sha, lead),
                           created_at=at(310))
    state0 = root / "fake-state-0.json"
    forge.save(state0)
    return TemplateCi(lang, root, origin, rendered, root_sha, prs, cfg, project_bytes, lead, state0, genv, fmt)


# -- utilidades ---------------------------------------------------------------------------


def runner_temp(tmp: Path) -> Path:
    path = tmp / "runner-temp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def findings(report: Mapping[str, Any], severity: str | None = None) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    for result in (report.get("data") or {}).get("results") or []:
        for f in result.get("findings") or []:
            if severity is None or f.get("severity") == severity:
                out.append({**f, "check": result.get("check")})
    return out


def statuses(report: Mapping[str, Any]) -> dict[str, str]:
    return {str(r["check"]): str(r["status"]) for r in (report.get("data") or {}).get("results") or []}


def envelope(path: Path, run: Run) -> dict[str, Any]:
    """Sobre de ``path`` (reporte de ``validate``), que valida contra ``result``."""
    report = read_envelope(path)
    assert report is not None, f"sin sobre legible en {path}\n{run.explain()}"
    assert validate_result(report) == [], report
    return report


def compile_yml(checkout: Path) -> Path:
    return checkout / ".github" / "workflows" / "compile.yml"


def step_script(workflow: Path, step_id: str | None = None, *, name: str | None = None) -> str:
    """``run`` del único paso con ese ``id`` o, con ``name``, cuyo nombre empieza así."""
    assert (step_id is None) != (name is None)

    def wanted(step: Mapping[str, Any]) -> bool:
        if step_id is not None:
            return step.get("id") == step_id
        return str(step.get("name", "")).startswith(str(name))

    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    found = [str(st["run"]) for job in doc["jobs"].values() for st in job.get("steps") or []
             if "run" in st and wanted(st)]
    if len(found) != 1:
        raise LookupError(f"{workflow.name}: {len(found)} pasos con run para {step_id or name!r}")
    return found[0]


def changed_diff_args(checkout: Path) -> list[str]:
    """Argumentos de git del ``git diff`` que escribe ``changed.txt`` en 'Política de la base'.

    Se leen del ``compile.yml`` del merge commit, el que corre GitHub en el PR, en lugar de
    copiarlos: la autoprueba ejecuta así las mismas opciones que el workflow, con
    ``--no-renames`` para que un renombre liste también su origen (REQ-CI-8).
    """
    commands = re.findall(r"\bgit diff\b[^>\n]*", step_script(compile_yml(checkout), POLICY_STEP))
    assert len(commands) == 1, commands
    argv = shlex.split(commands[0])
    assert argv[:2] == ["git", "diff"] and "--name-only" in argv and argv[-2:] == ["HEAD^1", "HEAD"], argv
    return argv[1:]


def build_plan(launcher: Launcher, ci: TemplateCi, pr: Pr, tmp: Path, mode: str, *,
               github_sha: str | None = None) -> tuple[Path, dict[str, str], list[tuple[str, str]]]:
    """Pasos 'Política de la base' y 'Plan de compilación' de compile.yml sobre el merge commit."""
    checkout = ci.checkout_merge(tmp / "compile", pr)
    rt = runner_temp(tmp)
    (rt / "policy").mkdir(exist_ok=True)
    (rt / "out").mkdir(exist_ok=True)
    rev = "HEAD^1" if mode == "pr" else "HEAD"
    (rt / "policy" / "project.yaml").write_bytes(
        subprocess.run(["git", "show", f"{rev}:project.yaml"], cwd=checkout, env=ci.git_env,
                       capture_output=True, check=True).stdout
    )
    changed = git(checkout, ci.git_env, *changed_diff_args(checkout)) + "\n" if mode == "pr" else ""
    (rt / "changed.txt").write_text(changed, encoding="utf-8", newline="\n")
    env = ci.env(tmp)
    if github_sha is not None:
        env["GITHUB_SHA"] = github_sha
    output = rt / "github-output"
    output.write_bytes(b"")
    run = launcher.run_line(
        workflow_command("ci build-plan"),
        {"MODE": mode, "RUNNER_TEMP": str(rt), "GITHUB_OUTPUT": str(output)},
        cwd=checkout, env=env,
    )
    assert run.returncode == 0, run.explain()
    pairs = parse_outputs(output.read_text(encoding="utf-8"))
    return checkout, env, pairs
