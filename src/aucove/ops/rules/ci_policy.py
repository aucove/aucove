# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Regla ``ci-policy`` (09-ci §4.1, REQ-CI-4..7, REQ-CI-29, REQ-CI-45; 10-seguridad SB06).

Revisa cada workflow de ``.github/workflows/`` del árbol validado (solo el primer
nivel de la carpeta, que es lo que lee GitHub) como datos: se leen con el cargador
estricto de ``config.yamlload`` y nunca se ejecuta nada. Hallazgos
``E_VALIDATION_CI_POLICY``:

- REQ-CI-4: ``permissions: {}`` en el nivel superior; ``permissions`` en cada job
  (``write-all`` no es mínimo) y ``timeout-minutes`` en cada job que no llama a un
  workflow reutilizable (GitHub no lo admite en esos jobs).
- REQ-CI-5 y REQ-SEC-47: todo ``uses:`` fijado a un SHA de 40 caracteres con la
  versión en un comentario; una imagen ``docker://``, ``container:`` o ``services:``
  solo con ``@sha256:``. Se
  exime solo el workflow reutilizable local ``./.github/workflows/<archivo>`` a nivel
  de job, que corre en el mismo commit y existe en el árbol; una acción local en un
  paso no se puede fijar y falla.
- REQ-CI-6: todo ``actions/checkout`` con ``persist-credentials: false`` y sin
  ``allow-unsafe-pr-checkout: true``.
- REQ-CI-7: ningún ``run:`` (ni el ``script`` de ``actions/github-script``) interpola
  con ``${{ }}`` datos que controla el autor del PR, también como objeto entero
  (``toJSON(github.event.pull_request)``), con notación de índice
  (``github.event['pull_request']['title']``) o como rama (``github.ref_name``).
- SB06: ``pull_request_target`` solo en ``.github/workflows/validate.yml``,
  ``.github/workflows/seal.yml`` (el sello de la entrega, 09-ci §7.4) y, desde H3,
  ``.github/workflows/editor-review.yml`` (la aprobación del editor, 09-ci §4.5), cumpliendo
  REQ-CI-29; ``workflow_run`` solo en ``editor-review.yml``. Un workflow con
  ``workflow_run`` también tiene token de escritura y la definición de la rama por defecto,
  así que sus checkouts se revisan igual. En un workflow con ``pull_request_target`` (o
  ``workflow_run``):

  - Es checkout del PR todo ``actions/checkout`` con un ``ref:`` o un ``repository:``
    que no es literal (una expresión ``${{ }}``, aunque nombre la base: la base de un
    PR puede ser cualquier rama del repositorio) o que nombra ``pull/<n>``.
  - Ese checkout va a un ``path:`` literal, distinto de la raíz y dentro del
    workspace, y solo corre para PR del mismo repositorio: el ``if:`` del paso o el del
    job es una conjunción con ``&&``, sin ``||`` ni ``!``, con
    ``github.event.pull_request.head.repo.full_name == github.repository`` como término.
  - Nada del job trabaja dentro de ese directorio (``working-directory``, también el
    de ``defaults.run`` del job o del workflow) ni lo nombra en ``run:``, ``shell:``,
    ``env:`` (``BASH_ENV`` ejecuta un archivo) o el ``with:`` de otra acción, salvo
    como valor de ``--tree`` o ``--repo-dir`` (las líneas de 09-ci §4.3 y §7.4) o en
    una prueba de existencia (``[ -d … ]``); el nombre suelto tras ``--mode`` es el modo
    de ``validate`` (``--mode pr``), no la carpeta. Cuentan también ``env:`` y
    ``defaults.run.shell`` del workflow.
- Ningún workflow usa el contexto ``secrets`` (``secrets.X``, ``secrets['X']``,
  ``toJSON(secrets)``, también en un ``if:`` sin ``${{ }}``) ni ``secrets:`` en un job
  (REQ-CI-3).

Las expresiones se normalizan antes de buscar: ``x['y']`` equivale a ``x.y``.
- REQ-CI-45: el ``core_pin`` del árbol (línea de REQ-ARCH-45) satisface ``core`` de la
  base.

El cargador estricto deja ``on`` como texto, pero la regla también acepta la clave
``True`` que produce un cargador YAML 1.1, para no perder los disparadores.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from functools import lru_cache

from ...config.project import core_pin_line
from ...config.yamlload import escape_pointer, line_for, load_with_lines
from ...domain.compat import satisfies
from ...domain.findings import Finding
from ...vcs.tree import is_regular_mode
from .base import RuleInput, RuleSpec, make_finding, unavailable

RULE_ID = "ci-policy"
CODE = "E_VALIDATION_CI_POLICY"
WORKFLOWS_DIR = ".github/workflows/"
VALIDATE_WORKFLOW = ".github/workflows/validate.yml"
SEAL_WORKFLOW = ".github/workflows/seal.yml"
EDITOR_REVIEW_WORKFLOW = ".github/workflows/editor-review.yml"
# Los únicos workflows con pull_request_target en v0.1 (10-seguridad SB06; editor-review desde H3).
PRT_WORKFLOWS: frozenset[str] = frozenset({VALIDATE_WORKFLOW, SEAL_WORKFLOW, EDITOR_REVIEW_WORKFLOW})
# Y el único con workflow_run (09-ci REQ-CI-60).
WORKFLOW_RUN_WORKFLOWS: frozenset[str] = frozenset({EDITOR_REVIEW_WORKFLOW})
PROJECT_FILE = "project.yaml"
MAX_SHOWN = 120  # caracteres de un valor del PR citado en un mensaje

_SHA_PINNED = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
_DOCKER_PINNED = re.compile(r"^docker://[^@\s]+@sha256:[0-9a-f]{64}$")
_IMAGE_PINNED = re.compile(r"^(?:docker://)?[^@\s$]+@sha256:[0-9a-f]{64}$")
_LOCAL_REUSABLE = re.compile(r"^\./\.github/workflows/([A-Za-z0-9_.-]+\.ya?ml)$")
_VERSION_COMMENT = re.compile(r"@[0-9a-f]{40}[\"']?\s+#\s*\S")
_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.S)
# El contexto secrets como identificador: secrets.X, secrets['X'], toJSON(secrets)...;
# no la propiedad de otro objeto (steps.secrets.outputs) ni un literal ('secrets').
_SECRETS = re.compile(r"(?<![\w.'\"-])secrets(?![\w-])")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_SPACE = r"\s*\.\s*"
# Índice con un literal: x['y'] y x["y"] equivalen a x.y en las expresiones de GitHub.
_INDEX = re.compile(r"""\s*\[\s*(?:'([^'\[\]]*)'|"([^"\[\]]*)")\s*\]""")
# Un objeto usado entero (toJSON(...), un índice que no es literal o el fin de la expresión).
_WHOLE = r"\b(?!\s*\.)"

# Datos que controla el autor del PR (título, cuerpo, rama, etiquetas, logins): REQ-CI-7
# exige pasarlos por variables de entorno, nunca interpolados en run:. Se buscan en la
# expresión ya normalizada (_normalize), así que la notación de índice no los oculta.
_UNTRUSTED = re.compile(
    r"\bgithub(?:"
    r"\s*(?:\)|,|$)"  # el contexto entero, p. ej. toJSON(github)
    r"|\s*\["  # índice dinámico sobre el contexto
    r"|" + _SPACE + r"(?:"
    r"head_ref\b|ref_name\b|ref\b|actor\b|triggering_actor\b"  # ref y ref_name: la rama en push
    r"|event" + _WHOLE  # el evento entero, p. ej. toJSON(github.event)
    + r"|event" + _SPACE + r"(?:pull_request|pull_request_target|issue|discussion)(?:" + _WHOLE + r"|" + _SPACE
    + r"(?:title|body|user|labels|assignees?|requested_reviewers|milestone"
    + r"|head(?:" + _WHOLE + r"|" + _SPACE + r"(?:ref|label|repo|user)\b))\b)"
    r"|event" + _SPACE + r"(?:comment|review|review_comment|release)(?:" + _WHOLE + r"|" + _SPACE
    + r"(?:body|user|name|tag_name)\b)"
    r"|event" + _SPACE + r"(?:head_commit|commits|label|sender|pusher|pages|inputs|changes)\b"
    r"|event" + _SPACE + r"workflow_run(?:" + _WHOLE + r"|" + _SPACE
    + r"(?:head_branch|head_commit|display_title|pull_requests|actor|triggering_actor)\b)"
    r"))",
    re.IGNORECASE,
)
# Un ref literal que nombra un PR (refs/pull/<n>/head, pull/<n>/merge); uno no literal ya cuenta
# como del PR (REQ-CI-29).
_PULL_REF = re.compile(r"\bpull/")
# La condición de mismo repositorio de REQ-CI-29, en los dos órdenes; tiene que ser un término
# entero de la conjunción del if: (_same_repo_guard).
_SAME_REPO = re.compile(
    r"github" + _SPACE + r"event" + _SPACE + r"pull_request" + _SPACE + r"head" + _SPACE + r"repo" + _SPACE
    + r"full_name\s*==\s*github" + _SPACE + r"repository"
    r"|github" + _SPACE + r"repository\s*==\s*github" + _SPACE + r"event" + _SPACE + r"pull_request" + _SPACE
    + r"head" + _SPACE + r"repo" + _SPACE + r"full_name",
    re.IGNORECASE,
)
_WRAPPED = re.compile(r"\s*\$\{\{(.*)\}\}\s*", re.S)  # un if: escrito entero dentro de ${{ }}
_LITERAL = re.compile(r"'(?:[^']|'')*'")  # cadena de una expresión ('' escapa la comilla)
_NOT = re.compile(r"!(?!=)")  # negación; != es una comparación
_MAX_GROUPS = 16  # paréntesis anidados que se abren para buscar la condición
_WORKSPACE = r"(?:\$\{\{\s*github\s*\.\s*workspace\s*\}\}|\$\{GITHUB_WORKSPACE\}|\$GITHUB_WORKSPACE)"
_WORKSPACE_PREFIX = re.compile(r"^" + _WORKSPACE + r"/?")
# Opciones con las que las líneas de 09-ci (§4.3 y §7.4) pasan el checkout del PR como datos.
PR_DATA_OPTIONS: tuple[str, ...] = ("--tree", "--repo-dir")
# Lo que puede preceder a una mención admitida del checkout del PR: el valor de una de esas
# opciones o el operando de una prueba de existencia ([ -d "$GITHUB_WORKSPACE/pr" ]).
_DATA_VALUE = re.compile(
    r"(?<![\w-])(?:" + "|".join(re.escape(o) for o in PR_DATA_OPTIONS) + r")(?:[ \t]*=[ \t]*|(?:[ \t]|\\\n)+)[\"']?\Z"
)
_EXISTS_TEST = re.compile(r"(?:\[\[?|(?<![\w-])test)(?:[ \t]+!)?[ \t]+-[A-Za-z][ \t]+[\"']?\Z")
# El nombre suelto, sin ruta, como valor de --mode es el modo de validate (--mode pr), no la carpeta.
_WORD_VALUE = re.compile(r"(?<![\w-])--mode(?:[ \t]*=[ \t]*|[ \t]+)[\"']?\Z")
_CONTEXT = 256  # caracteres antes de una mención en los que se busca ese contexto (tiempo acotado)

_MISSING = object()


def _shown(value: object) -> str:
    """Valor del PR para un mensaje: una línea, sin controles y recortado."""
    text = " ".join(_CONTROL.sub(" ", str(value)).split())
    return text if len(text) <= MAX_SHOWN else text[: MAX_SHOWN - 3] + "..."


class _Check:
    """Revisión de un workflow: acumula hallazgos con la línea de cada pointer."""

    def __init__(self, inp: RuleInput, path: str, lines: Mapping[str, int], raw_lines: Sequence[str]) -> None:
        self.inp = inp
        self.path = path
        self.lines = lines
        self.raw_lines = raw_lines
        self.findings: list[Finding] = []

    def add(self, pointer: str | None, key: str, **kw: object) -> None:
        detail = self.inp.t(f"validate.ci_policy.{key}", **kw)
        line = line_for(self.lines, pointer) if pointer is not None else None
        self.findings.append(make_finding(RULE, self.inp, CODE, severity="error", path=self.path, line=line,
                                          detail=detail))

    def raw_line(self, pointer: str) -> str:
        line = line_for(self.lines, pointer)
        if line is None or not 1 <= line <= len(self.raw_lines):
            return ""
        return self.raw_lines[line - 1]

    def has_version_comment(self, pointer: str, uses: str) -> bool:
        """REQ-CI-5: la línea del ``uses:`` lleva la versión en un comentario."""
        if _VERSION_COMMENT.search(self.raw_line(pointer)):
            return True
        if pointer in self.lines:
            return False
        # sin mapa de líneas (otro cargador): cualquier línea con ese uses: y su comentario
        return any(uses in raw and _VERSION_COMMENT.search(raw) for raw in self.raw_lines)


def _events(doc: Mapping[object, object]) -> set[str]:
    """Nombres de los disparadores; ``on`` o ``True`` (un cargador YAML 1.1 lee ``on`` como booleano)."""
    on = doc["on"] if "on" in doc else doc.get(True, _MISSING)
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(e) for e in on if isinstance(e, str)}
    if isinstance(on, Mapping):
        return {str(k) for k in on if isinstance(k, str)}
    return set()


def _strings(node: object, pointer: str = "") -> Iterator[tuple[str, str]]:
    """``(pointer, texto)`` de todos los valores de texto del documento."""
    stack: list[tuple[object, str]] = [(node, pointer)]
    while stack:
        current, ptr = stack.pop()
        if isinstance(current, str):
            yield ptr, current
        elif isinstance(current, Mapping):
            for key, value in current.items():
                stack.append((value, f"{ptr}/{escape_pointer(str(key))}"))
        elif isinstance(current, list):
            for i, value in enumerate(current):
                stack.append((value, f"{ptr}/{i}"))


def _normalize(expr: str) -> str:
    """``x['y']`` y ``x["y"]`` como ``x.y``: la misma propiedad en las dos notaciones."""
    return _INDEX.sub(lambda m: "." + (m.group(1) if m.group(1) is not None else m.group(2)), expr)


def _untrusted_expressions(text: str) -> list[str]:
    return [m.group(0) for m in _EXPRESSION.finditer(text) if _UNTRUSTED.search(_normalize(m.group(1)))]


def _expressions(pointer: str, value: str) -> list[str]:
    """Expresiones de un valor: las ``${{ }}`` y, en ``if:``, el valor entero (GitHub lo evalúa así)."""
    found = [m.group(1) for m in _EXPRESSION.finditer(value)]
    if pointer.endswith("/if") and not found:
        found.append(value)
    return found


def _is_false(value: object) -> bool:
    return value is False or (isinstance(value, str) and value.strip().lower() == "false")


def _is_true(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _workdir(value: object) -> str:
    """Carpeta relativa al workspace, normalizada (``./pr/`` y ``pr/tex/..`` son ``pr``); ``""`` es la raíz."""
    text = _WORKSPACE_PREFIX.sub("", str(value).strip()).strip("/")
    text = posixpath.normpath(text) if text else ""
    return "" if text == "." else text


def _checkout_dir(value: object) -> str | None:
    """``path:`` del checkout del PR si es una carpeta literal dentro del workspace y no su raíz."""
    if value is None:
        return None
    target = _workdir(value)
    if not target or "$" in target or target == ".." or target.startswith("../"):
        return None
    return target


def _pr_checkout(with_: Mapping[object, object]) -> bool:
    """REQ-CI-29: ¿el checkout puede traer contenido del PR?

    Sí si ``ref:`` o ``repository:`` no es literal: lo que da una expresión no se puede
    saber, y hasta la base de un PR es una rama que eligió su autor. Un literal solo si
    nombra ``pull/<n>``.
    """
    for key in ("ref", "repository"):
        value = with_.get(key)
        if value is not None and ("${{" in str(value) or _PULL_REF.search(str(value))):
            return True
    return False


def _conjuncts(expr: str) -> list[str] | None:
    """Términos de ``expr`` unidos por ``&&`` fuera de paréntesis; ``None`` si los paréntesis no cierran."""
    terms: list[str] = []
    depth = start = i = 0
    while i < len(expr):
        if expr[i] == "(":
            depth += 1
        elif expr[i] == ")":
            depth -= 1
            if depth < 0:
                return None
        elif depth == 0 and expr.startswith("&&", i):
            terms.append(expr[start:i])
            start = i + 2
            i += 1
        i += 1
    if depth:
        return None
    terms.append(expr[start:])
    return terms


def _requires_same_repo(expr: str, groups: int = 0) -> bool:
    """¿Algún término de la conjunción es la condición de mismo repositorio?

    Un término entre paréntesis de agrupación se abre (hasta ``_MAX_GROUPS``); los de una
    llamada no, porque su valor puede no depender de la condición (``format('{0}', …)``).
    """
    terms = _conjuncts(expr)
    for term in terms or ():
        term = term.strip()
        if _SAME_REPO.fullmatch(term):
            return True
        # Si el paréntesis del principio no cierra al final, _conjuncts del interior da None.
        if groups < _MAX_GROUPS and term.startswith("(") and term.endswith(")") \
                and _requires_same_repo(term[1:-1], groups + 1):
            return True
    return False


def _same_repo_guard(value: object) -> bool:
    """REQ-CI-29: ¿el ``if:`` solo deja pasar PR del mismo repositorio?

    Tiene que ser una conjunción con la comparación de ``_SAME_REPO`` como término, sin
    ``||`` ni ``!`` en ninguna parte: con ellos la comparación deja de obligar
    (``… || true``). Un valor que mezcla texto y ``${{ }}`` se evalúa como una cadena,
    siempre verdadera, y tampoco cuenta.
    """
    if not isinstance(value, str):
        return False
    wrapped = _WRAPPED.fullmatch(value)
    expr = wrapped.group(1) if wrapped is not None else value
    if "${{" in expr or "}}" in expr:
        return False
    expr = _LITERAL.sub("''", _normalize(expr))
    if "||" in expr or _NOT.search(expr):
        return False
    return _requires_same_repo(expr)


@lru_cache(maxsize=64)
def _mention(directory: str) -> re.Pattern[str]:
    """``directory`` como ruta: desde el workspace, relativa (``pr``, ``./pr``) o tras ``../``.

    No es mención el mismo nombre dentro de otra ruta (``$RUNNER_TEMP/pr``), de otra palabra
    (``aucove``, ``--pr``, ``pr.json``) ni de una variable (``$PR_NUMBER``). El grupo
    incluye el resto de la ruta, para el mensaje.
    """
    return re.compile(
        r"(?:" + _WORKSPACE + r"[\"']?/+(?:\./+)*|(?<![\w./${~@%+-])(?:\./+)*|(?<=\.\./))"
        + re.escape(directory) + r"(?![\w.@%+~=-])[^\s\"'`;&|<>()]*",
        re.IGNORECASE,
    )


def _pr_names(pr_dirs: Sequence[str]) -> tuple[str, ...]:
    """Las carpetas del checkout del PR y su último segmento (``cd work && ./pr/x`` con ``path: work/pr``)."""
    names: list[str] = []
    for directory in pr_dirs:
        for name in (directory, directory.rsplit("/", 1)[-1]):
            if name not in names:
                names.append(name)
    return tuple(names)


def _pr_dir_use(text: str, names: Sequence[str]) -> str | None:
    """La primera mención del checkout del PR en ``text`` que no es un dato, o ``None``."""
    first: re.Match[str] | None = None
    for name in names:
        for m in _mention(name).finditer(text):
            before = text[max(0, m.start() - _CONTEXT) : m.start()]
            if _DATA_VALUE.search(before) or _EXISTS_TEST.search(before):
                continue
            if m.group(0).casefold() == name.casefold() and _WORD_VALUE.search(before):
                continue
            if first is None or m.start() < first.start():
                first = m
            break
    return None if first is None else first.group(0)


def _run_default(defaults: object, key: str) -> object:
    """``defaults.run.<key>`` de un job o del workflow, o ``None``."""
    if isinstance(defaults, Mapping) and isinstance(defaults.get("run"), Mapping):
        return defaults["run"].get(key)
    return None


def _env_texts(env: object, pointer: str, label: str) -> Iterator[tuple[str, str, str]]:
    if isinstance(env, Mapping):
        for name, value in env.items():
            if isinstance(value, str):
                yield f"{pointer}/{escape_pointer(str(name))}", f"{label}.{name}", value


def _job_texts(jp: str, label: str, job: Mapping[object, object], steps: Sequence[object]) -> Iterator[tuple[str, str, str]]:
    """``(pointer, campo, texto)`` de lo que el job ejecuta o entrega a sus procesos (REQ-CI-29).

    ``env`` y ``defaults.run.shell`` del job y, en cada paso, ``run``, ``shell``, ``env`` y el
    ``with`` de las acciones que no son ``actions/checkout`` (no se sabe qué hacen con una ruta).
    """
    yield from _env_texts(job.get("env"), f"{jp}/env", f"{label}.env")
    shell = _run_default(job.get("defaults"), "shell")
    if isinstance(shell, str):
        yield f"{jp}/defaults/run/shell", f"{label}.defaults.run.shell", shell
    for i, step in enumerate(steps):
        if not isinstance(step, Mapping):
            continue
        sp, sl = f"{jp}/steps/{i}", f"{label}.steps[{i}]"
        for key in ("run", "shell"):
            value = step.get(key)
            if isinstance(value, str):
                yield f"{sp}/{key}", f"{sl}.{key}", value
        yield from _env_texts(step.get("env"), f"{sp}/env", f"{sl}.env")
        uses, with_ = step.get("uses"), step.get("with")
        if isinstance(uses, str) and uses.split("@", 1)[0].lower() != "actions/checkout" and isinstance(with_, Mapping):
            for key, value in with_.items():
                if isinstance(value, str):
                    yield f"{sp}/with/{escape_pointer(str(key))}", f"{sl}.with.{key}", value


def _check_pr_dir_uses(chk: _Check, texts: Iterable[tuple[str, str, str]], pr_dirs: Sequence[str]) -> None:
    names = _pr_names(pr_dirs)
    for pointer, field, text in texts:
        use = _pr_dir_use(text, names)
        if use is not None:
            chk.add(pointer, "prt_pr_dir", field=_shown(field), dir=_shown(use))


def _check_uses_step(chk: _Check, pointer: str, uses: str) -> None:
    if uses.startswith("./"):
        chk.add(pointer, "uses_local", uses=_shown(uses))
    elif uses.startswith("docker://"):
        if not _DOCKER_PINNED.match(uses):
            chk.add(pointer, "uses_docker", uses=_shown(uses))
    elif not _SHA_PINNED.match(uses):
        chk.add(pointer, "uses_unpinned", uses=_shown(uses))
    elif not chk.has_version_comment(pointer, uses):
        chk.add(pointer, "uses_comment", uses=_shown(uses))


def _check_reusable(chk: _Check, pointer: str, uses: str, entries: Mapping[str, str]) -> None:
    local = _LOCAL_REUSABLE.match(uses)
    if local is not None:
        if not is_regular_mode(entries.get(WORKFLOWS_DIR + local.group(1))):
            chk.add(pointer, "reusable_missing", uses=_shown(uses))
        return
    if not _SHA_PINNED.match(uses):
        chk.add(pointer, "uses_unpinned", uses=_shown(uses))
    elif not chk.has_version_comment(pointer, uses):
        chk.add(pointer, "uses_comment", uses=_shown(uses))


def _check_images(chk: _Check, jp: str, job: Mapping[object, object]) -> None:
    """REQ-SEC-47: ``container`` y ``services.*`` con la imagen fijada por ``@sha256:``."""
    targets: list[tuple[str, object]] = []
    container = job.get("container")
    if isinstance(container, Mapping):
        targets.append((f"{jp}/container/image", container.get("image")))
    elif container is not None:
        targets.append((f"{jp}/container", container))
    services = job.get("services")
    if isinstance(services, Mapping):
        for name, service in services.items():
            sp = f"{jp}/services/{escape_pointer(str(name))}"
            if isinstance(service, Mapping):
                targets.append((f"{sp}/image", service.get("image")))
            else:
                targets.append((sp, service))
    for pointer, image in targets:
        if image is None:
            continue
        text = str(image).strip()
        if not _IMAGE_PINNED.match(text):
            chk.add(pointer, "image_unpinned", image=_shown(text))


def _check_job(
    chk: _Check, job_id: str, job: Mapping[object, object], *, prt: bool, entries: Mapping[str, str],
    workflow_defaults: object = None,
) -> list[str]:
    """Revisa un job; devuelve las carpetas de sus checkouts del PR (con ``pull_request_target`` o
    ``workflow_run``: ``prt``)."""
    jp = f"/jobs/{escape_pointer(job_id)}"
    shown_job = _shown(job_id)
    _check_images(chk, jp, job)
    perms = job.get("permissions", _MISSING)
    if perms is _MISSING:
        chk.add(jp, "job_permissions", job=shown_job)
    elif isinstance(perms, str) and perms.strip().lower() == "write-all":
        chk.add(f"{jp}/permissions", "job_write_all", job=shown_job)
    if "secrets" in job:
        chk.add(f"{jp}/secrets", "secrets_inherit", job=shown_job)
    uses = job.get("uses")
    if uses is not None:
        _check_reusable(chk, f"{jp}/uses", str(uses), entries)
    elif "timeout-minutes" not in job:
        chk.add(jp, "job_timeout", job=shown_job)
    steps = job.get("steps")
    if not isinstance(steps, list):
        steps = []
    pr_dirs: list[str] = []
    for i, step in enumerate(steps):
        if not isinstance(step, Mapping):
            continue
        sp = f"{jp}/steps/{i}"
        with_ = step.get("with") if isinstance(step.get("with"), Mapping) else {}
        assert isinstance(with_, Mapping)
        step_uses = step.get("uses")
        if isinstance(step_uses, str):
            _check_uses_step(chk, f"{sp}/uses", step_uses)
            action = step_uses.split("@", 1)[0].lower()
            if action == "actions/checkout":
                if not _is_false(with_.get("persist-credentials", _MISSING)):
                    chk.add(f"{sp}/uses", "checkout_credentials", job=shown_job)
                if _is_true(with_.get("allow-unsafe-pr-checkout", False)):
                    chk.add(f"{sp}/with/allow-unsafe-pr-checkout", "checkout_unsafe", job=shown_job)
                if prt and _pr_checkout(with_):
                    target = _checkout_dir(with_.get("path"))
                    if target is None:
                        chk.add(f"{sp}/uses", "prt_checkout_path", job=shown_job)
                    else:
                        pr_dirs.append(target)
                    if not (_same_repo_guard(step.get("if")) or _same_repo_guard(job.get("if"))):
                        chk.add(f"{sp}/uses", "prt_checkout_same_repo", job=shown_job)
            elif action == "actions/github-script" and isinstance(with_.get("script"), str):
                for expr in _untrusted_expressions(str(with_["script"])):
                    chk.add(f"{sp}/with/script", "injection", expr=_shown(expr))
        run = step.get("run")
        if isinstance(run, str):
            for expr in _untrusted_expressions(run):
                chk.add(f"{sp}/run", "injection", expr=_shown(expr))
    if pr_dirs:
        # REQ-CI-29: nada del job trabaja dentro del checkout del PR ni lo usa salvo como dato.
        _check_pr_dir_uses(chk, _job_texts(jp, f"jobs.{job_id}", job, steps), pr_dirs)
        default_dir = _run_default(job.get("defaults"), "working-directory")
        if default_dir is None:
            default_dir = _run_default(workflow_defaults, "working-directory")
        for i, step in enumerate(steps):
            if not isinstance(step, Mapping):
                continue
            # defaults.run solo vale para los pasos run:
            wd = step.get("working-directory", default_dir if "run" in step else None)
            if wd is None:
                continue
            target = _workdir(wd)
            if any(target == d or target.startswith(d + "/") for d in pr_dirs):
                chk.add(f"{jp}/steps/{i}", "prt_workdir", job=shown_job, dir=_shown(wd))
    return pr_dirs


def _check_workflow(inp: RuleInput, path: str, text: str, entries: Mapping[str, str]) -> list[Finding]:
    doc, lines, syntax, norm = load_with_lines(text, file=path, t=inp.t, check=RULE_ID)
    if syntax:
        first = syntax[0]
        detail = inp.t("validate.ci_policy.yaml", problem=first.message.rstrip("."))
        return [make_finding(RULE, inp, CODE, severity="error", path=path, line=first.line, detail=detail)]
    return check_document(inp, path, doc, entries, lines=lines, raw_lines=(norm or "").split("\n"))


def check_document(
    inp: RuleInput,
    path: str,
    doc: object,
    entries: Mapping[str, str],
    *,
    lines: Mapping[str, int] | None = None,
    raw_lines: Sequence[str] = (),
) -> list[Finding]:
    """Hallazgos de un workflow ya cargado (por el cargador estricto o por uno YAML 1.1)."""
    chk = _Check(inp, path, lines or {}, raw_lines)
    if not isinstance(doc, Mapping):
        chk.add(None, "not_mapping")
        return chk.findings
    perms = doc.get("permissions", _MISSING)
    if perms is _MISSING:
        chk.add(None, "top_permissions_missing")
    elif not (isinstance(perms, Mapping) and not perms):
        chk.add("/permissions", "top_permissions_not_empty")
    events = _events(doc)
    on_pointer = "/on" if "on" in doc else None
    if "pull_request_target" in events and path not in PRT_WORKFLOWS:
        chk.add(on_pointer, "pull_request_target")
    if "workflow_run" in events and path not in WORKFLOW_RUN_WORKFLOWS:
        chk.add(on_pointer, "workflow_run")
    # Con los dos, el job tiene token de escritura: REQ-CI-29 para sus checkouts.
    prt = "pull_request_target" in events or "workflow_run" in events
    for pointer, value in _strings(doc):
        for expr in _expressions(pointer, value):
            if _SECRETS.search(_normalize(expr)):
                chk.add(pointer, "secrets")
    jobs = doc.get("jobs")
    pr_dirs: list[str] = []
    if not isinstance(jobs, Mapping) or not jobs:
        chk.add(None, "no_jobs")
    else:
        for job_id, job in jobs.items():
            if not isinstance(job, Mapping):
                chk.add(f"/jobs/{escape_pointer(str(job_id))}", "job_invalid", job=_shown(job_id))
                continue
            pr_dirs.extend(_check_job(chk, str(job_id), job, prt=prt, entries=entries,
                                      workflow_defaults=doc.get("defaults")))
    if pr_dirs:
        # env y defaults.run.shell del workflow llegan a todos los jobs (REQ-CI-29).
        texts = list(_env_texts(doc.get("env"), "/env", "env"))
        shell = _run_default(doc.get("defaults"), "shell")
        if isinstance(shell, str):
            texts.append(("/defaults/run/shell", "defaults.run.shell", shell))
        _check_pr_dir_uses(chk, texts, pr_dirs)
    chk.findings.sort(key=lambda f: (f.line or 0, f.message))
    return chk.findings


def workflow_paths(entries: Mapping[str, str]) -> list[str]:
    """Workflows que lee GitHub: archivos ``.yml`` o ``.yaml`` directamente en ``.github/workflows/``."""
    out = []
    for path in entries:
        if not path.startswith(WORKFLOWS_DIR):
            continue
        name = path[len(WORKFLOWS_DIR):]
        if "/" not in name and name.lower().endswith((".yml", ".yaml")):
            out.append(path)
    return sorted(out)


def _check_core_pin(inp: RuleInput) -> list[Finding]:
    """REQ-CI-45: el ``core_pin`` del árbol satisface ``core`` de la base."""
    assert inp.tree is not None
    text = inp.tree.read_text(PROJECT_FILE)
    if text is None or inp.policy is None:
        return []  # sin project.yaml legible lo informa la regla schemas
    norm = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    keys = ("core_pin", '"core_pin"', "'core_pin'")
    line = next((i for i, raw in enumerate(norm.split("\n"), 1) if raw.startswith(keys)), None)
    pin = core_pin_line(text)
    detail: str | None = None
    if pin is None:
        detail = inp.t("validate.ci_policy.core_pin_line")
    elif not satisfies(pin, inp.policy.project.core):
        detail = inp.t("validate.ci_policy.core_pin_core", pin=pin, core=_shown(inp.policy.project.core))
    if detail is None:
        return []
    return [make_finding(RULE, inp, CODE, severity="error", path=PROJECT_FILE, line=line, detail=detail)]


def _run(inp: RuleInput) -> Sequence[Finding]:
    tree = inp.tree
    if tree is None:
        return [unavailable(RULE, inp, inp.t("validate.reason.no_tree"))]
    entries = tree.entries()
    findings: list[Finding] = []
    for path in workflow_paths(entries):
        if not is_regular_mode(entries.get(path)):
            findings.append(make_finding(RULE, inp, CODE, severity="error", path=path,
                                         detail=inp.t("validate.ci_policy.not_regular")))
            continue
        text = tree.read_text(path)
        if text is None:
            findings.append(make_finding(RULE, inp, CODE, severity="error", path=path,
                                         detail=inp.t("validate.ci_policy.unreadable")))
            continue
        findings.extend(_check_workflow(inp, path, text, entries))
    findings.extend(_check_core_pin(inp))
    return findings


RULE = RuleSpec(
    id=RULE_ID,
    modes=frozenset({"tree", "pr", "base"}),
    run=_run,
    codes={CODE: frozenset({"detail"})},
)

RULES: tuple[RuleSpec, ...] = (RULE,)
