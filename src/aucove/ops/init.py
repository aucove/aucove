# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove init`` (05 §9.3; 06-permisos §4 y §5; 01 §10.6 y §10.7; 00-marco D-57 y D-62).

Solo humana y sin MCP (D-09). Dos caminos:

``init --regenerate`` (01 REQ-ARCH-87)
    Exige que la versión instalada sea ``core_pin`` (``E_CONFIG_CORE_MISMATCH``), no llama a la
    forja y solo escribe en el árbol de trabajo los archivos ``generated``
    (``init_files.plan_generated``). Actor: el ``lead`` de la base, por la identidad en caché.
    Si algo cambió, ``W_STATE_INIT_FILES_PENDING``: se entrega desde una tarea del responsable.

``init``
    1. Lecturas. ``forge.repo`` sale de ``origin`` (en un repo recién creado, ``project.yaml``
       todavía nombra el de la plantilla); la identidad y las capacidades se detectan de nuevo
       (REQ-FORGE-18). La base tiene que ser la rama por defecto (REQ-PERM-24). El actor tiene
       que administrar el repositorio y ser el ``lead`` de ``roles.yaml`` (o este, el de ejemplo,
       que ``init`` completa con él); si la base ya está protegida, también el de la base (D-08).
    2. Las licencias de 11-trazabilidad §7.3 (``license_choices``): las de ``project.yaml`` o, si
       no tiene, el titular y las tres que responde la persona, con las sugerencias de la tabla
       según la visibilidad del repositorio; un identificador inválido se vuelve a preguntar
       (hasta tres veces) y, para una licencia cuyo texto no trae el paquete, se pide la ruta
       del texto (00-marco D-64). Sin respuesta válida, ``E_VALIDATION_INPUT`` sin escribir nada.
       Luego, el plan de archivos (``init_files.plan_files``) y si se suben a la base: solo si
       no está protegida; si lo está, quedan en el árbol con ``W_STATE_INIT_FILES_PENDING``.
    3. La fase 2 (``init <owner>/<repo>``). En ``advisory``, la frase de REQ-PERM-40 (si no,
       ``E_FORGE_CAPABILITY_MISSING``), que queda en ``forge.advisory_acceptance``
       (REQ-PERM-41, enmendado: la decisión se registra desde la primera tarea). Sin suplente,
       en ``group`` y ``enforced``, la confirmación del bypass de REQ-PERM-19 (si no, no hay
       actor de bypass y los PR del responsable quedan bloqueados, que es válido).
    4. El token de administración, sin eco (10-seguridad REQ-SEC-30), de la misma cuenta.
    5. En el orden de 06 §4.1 y con 1 s entre escrituras (REQ-FORGE-50), cada paso lee, compara
       y escribe solo si difiere (REQ-PERM-25): ajustes del repositorio (§4.2, REQ-TRACE-35),
       Actions (§4.3, con la política de REQ-PERM-56), colaboradores (§4.4), etiquetas (§4.5),
       archivos (commit con ``AI-Use: none`` y push a la base), rulesets (solo en ``enforced``:
       §4.7, §4.7b y REQ-PERM-59 con ``mode: individual``) y la verificación de REQ-PERM-38.
       Los ajustes de la organización solo se informan (§4.10).
    6. La tarea de preparación (00-marco D-70): si el repositorio no tiene tareas y la
       preparación queda pendiente en el árbol que deja ``init`` (REQ-OPS-69), crea la tarea del
       ``lead`` con sus partes y le quita ``state:proposed``, que la deja aceptada. La busca
       antes por su marcador ``op`` (``id=init-setup``), así que una segunda ejecución no escribe.

``data_init`` lleva cada archivo, etiqueta y protección con su acción, y ``pending_steps``,
lo que falta hacer a mano, en el idioma del proyecto.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, TypeVar

from .. import _meta
from ..config.datafiles import license_text, spdx_licenses, spdx_version
from ..config.project import parse_project
from ..domain.checks import required_checks
from ..domain.licenses import ALL_RIGHTS_RESERVED, CATEGORIES, LICENSE_ID, OPTIONS, LicenseSet, is_open, suggested
from ..domain.taskbody import render_task_body
from ..domain.compat import same_version
from ..domain.text import same_login
from ..domain.trailers import commit_message, trailer_block
from ..forge.base import Capabilities, ForgeAdmin, ForgeError, ForgeUnsupported, ForgeValidationError
from ..forge.cache import CAPABILITIES_FILE
from ..i18n import LANGS, Translator, translator
from ..vcs.tree import worktree
from .context import ForgeTarget, env_flag, testing
from .doctor import github_repo
from .gitwork import fetch_base, guard_remote, push_ref
from .init_files import Change, Choices, FilePlan, plan_files, plan_generated, project_name
from .protections import (
    BASE_RULESET,
    EVIDENCE_ENVIRONMENT,
    EVIDENCE_SECRET,
    LABELS,
    ORG_RECOMMENDED,
    TASK_RULESET,
    WORKFLOWS_RULESET,
    actions_restriction,
    base_ruleset,
    covers,
    desired_settings,
    environments_available,
    labels_for,
    missing_rules,
    settings_changes,
    task_ruleset,
    template_actions,
    workflows_ruleset,
)
from .result import OpError, Result, WarningObj, make_warning, ok
from .rules.roles_sync import tree_reader
from .setup import PLAN_FILE, setup_pending
from .snapshot import TASK_LABEL
from .steps import Steps
from .sync_roles import check_targets, delete_file, write_file
from .workspace import dirty

if TYPE_CHECKING:
    from pathlib import Path

    from ..config.roles import Roles
    from ..forge.base import ForgeAdapter, Issue
    from ..vcs.git import Git
    from .context import OpContext

__all__ = ["LABELS", "OP", "REGENERATE_STEPS", "STEPS", "WRITE_GAP_S", "render_text", "run"]

OP = "init"
STEPS = ("repo_settings", "actions", "collaborators", "labels", "files", "push", "rulesets", "verify", "setup_task")
REGENERATE_STEPS = ("files",)
WRITE_GAP_S = 1.0  # 08 REQ-FORGE-50: al menos 1 s entre escrituras de un lote
RETENTION_DAYS = {"private": 400, "internal": 400, "public": 90}  # 06 REQ-PERM-57
SETUP_OP_ID = "init-setup"  # 05 §9.3: marcador op de la tarea de preparación (00-marco D-70)
SETUP_DONE = ("plan", "roles", "data", "validate")  # criterios de terminado, en init.setup_task
PROPOSED = "state:proposed"
_STAFF_ROLES = frozenset({"admin", "maintain", "write"})
T = TypeVar("T")


ASK_TRIES = 3  # respuestas inválidas seguidas antes de E_VALIDATION_INPUT
MAX_LICENSE_TEXT = 1024 * 1024  # texto de una licencia que da la persona (00-marco D-64)
HOLDER_MAX = 200  # project.schema.json: licenses.holder


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


class _Writer:
    """Escrituras de la forja espaciadas al menos ``WRITE_GAP_S`` (REQ-FORGE-50)."""

    def __init__(self) -> None:
        self._last: float | None = None

    def __call__(self, fn: Callable[[], T]) -> T:
        if self._last is not None:
            wait = WRITE_GAP_S - (time.monotonic() - self._last)
            if wait > 0:
                _sleep(wait)
        try:
            return fn()
        finally:
            self._last = time.monotonic()


# -- la operación --------------------------------------------------------------------------------


@dataclass
class _Run:
    """Lo que acumula una ejecución de ``init``."""

    ctx: OpContext
    lang: Translator  # el idioma del proyecto (D-02)
    repo: str
    warnings: list[WarningObj] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    protections: list[dict[str, object]] = field(default_factory=list)
    labels: list[dict[str, object]] = field(default_factory=list)

    def warn(self, code: str, details: dict[str, object] | None = None) -> None:
        if all(w.code != code for w in self.warnings):
            self.warnings.append(make_warning(code, self.ctx.t(), details=details))

    def protection(self, ident: str, action: str, detail: str | None = None) -> None:
        self.protections.append({"id": ident, "action": action, "detail": detail})


def _check(name: str, value: object, allowed: Sequence[str]) -> None:
    if value is not None and value not in allowed:
        raise OpError("E_VALIDATION_INPUT", details={"errors": [{"param": name, "reason": "enum"}]})


def run(ctx: OpContext, *, language: object = None, format: object = None,  # noqa: A002 (flag --format, 05 §9.3)
        disclosure: object = None, individual: object = False, regenerate: object = False) -> Result:
    """``init`` (05 §9.3) o ``init --regenerate`` (01 REQ-ARCH-87)."""
    _check("language", language, LANGS)
    _check("format", format, ("latex", "none"))
    _check("disclosure", disclosure, ("private", "milestones", "open"))
    for name, value in (("individual", individual), ("regenerate", regenerate)):
        if not isinstance(value, bool):
            raise OpError("E_VALIDATION_INPUT", details={"errors": [{"param": name, "reason": "type"}]})
    if regenerate:
        given = [n for n, v in (("language", language), ("format", format), ("disclosure", disclosure)) if v is not None]
        if individual:
            given.append("individual")
        if given:
            raise OpError("E_VALIDATION_INPUT",
                          details={"errors": [{"param": "regenerate", "reason": "exclusive", "with": given}]})
        return _regenerate(ctx)
    return _init(ctx, language, format, disclosure, bool(individual))  # type: ignore[arg-type]


def _apply_files(root: Path, changes: Sequence[Change]) -> None:
    for change in changes:
        if change.action == "deleted":
            delete_file(root, change.path)
        else:
            assert change.content is not None
            write_file(root, change.path, change.content)


def _files_data(changes: Sequence[Change], *, pending: bool) -> list[dict[str, object]]:
    return [{"path": c.path, "action": "pending" if pending else c.action} for c in changes]


def _regenerate(ctx: OpContext) -> Result:
    """``init --regenerate``: los ``generated`` del árbol de trabajo, sin la forja (REQ-ARCH-87)."""
    project = ctx.project()
    assert project is not None
    git = ctx.git()
    snap = ctx.base()
    cfg = snap.project
    lang = translator(cfg.language) if cfg.language in LANGS else ctx.t()
    if not same_version(_meta.__version__, cfg.core_pin):
        raise OpError("E_CONFIG_CORE_MISMATCH",
                      details={"installed": _meta.__version__, "core_pin": cfg.core_pin, "reason": "regenerate"})
    ident = ctx.store().identity(token_fp=None)
    if ident is None:
        raise OpError("E_AUTH_NOT_AUTHENTICATED", details={"reason": "identity_cache_missing"})
    person = snap.roles.identify(ident.login, ident.id).person
    if person is None or not same_login(person.login, snap.roles.lead):
        raise OpError("E_PERM_ROLE_REQUIRED", details={"login": ident.login, "required_role": ["lead"]})
    tree = worktree(project.root, git=git)
    read, _exists = tree_reader(tree)
    changes = plan_generated(read, tree.entries().keys(), t=lang)
    check_targets(project.root, [c.path for c in changes if c.action != "deleted"], tree.entries())
    steps = Steps(ctx, OP, REGENERATE_STEPS, deadline_s=None)  # 05 REQ-OPS-22: init no tiene plazo
    if steps.dry_run:
        for change in changes:
            steps.effect("file_write", change.path, lang(f"init.effect.{change.action}", path=change.path))
        return steps.dry_result()
    warnings: list[WarningObj] = []
    if changes:
        summary = lang("init.regenerate.summary", repo=cfg.forge_repo, paths=", ".join(c.path for c in changes))
        ctx.confirm_human(OP, cfg.forge_repo, summary)
        steps.run("files", lambda: _apply_files(project.root, changes))
        warnings.append(make_warning("W_STATE_INIT_FILES_PENDING", ctx.t(), details={"paths": [c.path for c in changes]}))
    else:
        steps.skip("files", "unchanged")
    data = {
        "repo": cfg.forge_repo,
        "profile": _cached_profile(ctx),
        "advisory_accepted": cfg.advisory_acceptance is not None,
        "files": _files_data(changes, pending=False),
        "labels": [],
        "protections": [],
        "pushed": False,
        "setup_task": None,  # --regenerate no llama a la forja (REQ-ARCH-87)
        "pending_steps": [lang("init.pending.regenerate")] if changes else [],
    }
    return ok(OP, data, warnings)


def _cached_profile(ctx: OpContext) -> str:
    """El perfil de la caché de capacidades; sin ella, ``advisory`` (08 REQ-FORGE-21)."""
    try:
        data = ctx.store().read(CAPABILITIES_FILE)
    except Exception:  # noqa: BLE001 - sin caché legible vale el perfil que no promete nada
        return "advisory"
    profile = data.get("profile") if isinstance(data, dict) else None
    return profile if profile in ("enforced", "advisory") else "advisory"


def _fake_forge(ctx: OpContext) -> bool:
    """Pruebas: un adaptador inyectado o el falso de 08 REQ-FORGE-62, con un ``origin`` local."""
    env = ctx.inv.env
    return ctx.injected_forge or (testing(env) and env_flag(env, "FORGE_ADAPTER") == "fake")


def _origin_repo(ctx: OpContext, git: Git) -> str:
    """``owner/name`` del ``origin``, que será ``forge.repo`` (03 REQ-SCHEMA-18)."""
    url = git.remote_url("origin")
    if url is None:
        raise OpError("E_CONFIG_REMOTE_MISMATCH", details={"remote": "origin", "reason": "missing"})
    repo = github_repo(url)
    if repo is not None and not ctx.injected_forge:
        ctx.use_forge_target(ForgeTarget("github", repo))
        return repo
    if not _fake_forge(ctx):
        raise OpError("E_CONFIG_REMOTE_MISMATCH", details={"remote": "origin", "reason": "not_github"})
    forge = ctx.forge()
    return f"{forge.repo.owner}/{forge.repo.name}"


def _tree_project(read: Callable[[str], bytes | None]) -> Any:
    raw = read("project.yaml")
    if raw is None:
        raise OpError("E_CONFIG_NOT_A_PROJECT")
    cfg, findings = parse_project(raw)
    if cfg is None:
        errors = [f for f in findings if f.severity == "error"]
        details: dict[str, object] = {"path": "project.yaml", "reason": "project_invalid"}
        if errors:
            details["code"] = errors[0].code
        raise OpError("E_CONFIG_INVALID", details=details)
    return cfg


def valid_license(value: str) -> bool:
    """Un identificador vigente de la lista de SPDX empaquetada o un ``LicenseRef-*`` (REQ-TRACE-62)."""
    if not LICENSE_ID.fullmatch(value) or len(value) > 100:
        return False
    return value.startswith("LicenseRef-") or spdx_licenses().get(value) is False


def _ask_holder(ctx: OpContext, lang: Translator, default: str) -> str:
    holder = default
    for _ in range(ASK_TRIES):
        holder = ctx.ask_text(lang("init.license.holder", default=default), default)
        if holder.isprintable() and 0 < len(holder) <= HOLDER_MAX:
            return holder
    raise OpError("E_VALIDATION_INPUT", details={"errors": [{"param": "licenses.holder", "reason": "invalid"}]})


def _ask_license(ctx: OpContext, lang: Translator, cat: str, default: str) -> str:
    prompt = lang("init.license.ask", category=lang(f"init.license.category.{cat}"), default=default,
                  options=", ".join(OPTIONS[cat]))  # type: ignore[index]
    value = default
    for _ in range(ASK_TRIES):
        value = ctx.ask_text(prompt, default)
        if valid_license(value):
            return value
        prompt = lang("init.license.invalid", value=value, version=spdx_version()) + "\n" + prompt
    raise OpError("E_VALIDATION_INPUT", details={"errors": [{"param": f"licenses.{cat}", "reason": "spdx"}]})


def _read_license_file(ctx: OpContext, raw: str) -> bytes | None:
    """El texto de una licencia desde una ruta que tecleó la persona: UTF-8, no vacío, hasta 1 MiB."""
    from pathlib import Path

    path = Path(raw)
    if not path.is_absolute():
        path = Path(ctx.inv.cwd) / path
    try:
        if not path.is_file() or path.stat().st_size > MAX_LICENSE_TEXT:
            return None
        text = path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    if not text.strip():
        return None
    return text.replace("\r\n", "\n").encode("utf-8")


def _ask_license_text(ctx: OpContext, lang: Translator, license_id: str) -> bytes:
    """D-64: el paquete no trae el texto; la persona da su ruta o ``init`` falla sin escribir nada."""
    prompt = lang("init.license.text_path", id=license_id)
    for _ in range(ASK_TRIES):
        raw = ctx.ask_text(prompt, "")
        if not raw:
            break
        data = _read_license_file(ctx, raw)
        if data is not None:
            return data
        prompt = lang("init.license.text_bad", path=raw) + "\n" + lang("init.license.text_path", id=license_id)
    raise OpError("E_VALIDATION_INPUT", details={"errors": [
        {"param": "licenses", "reason": "text_missing", "license": license_id}]})


def license_choices(ctx: OpContext, current: Any, repo: str, visibility: str, lang: Translator,
                    entries: Iterable[str]) -> tuple[LicenseSet, dict[str, bytes], bool]:
    """Las licencias de 11 §7.3: las de ``project.yaml`` o, si no tiene, las que responde la persona.

    Devuelve las licencias, los textos que dio para las que el paquete no trae (00-marco D-64) y si son
    nuevas (las preguntó). Las sugerencias son las de la tabla según la visibilidad del repositorio.
    """
    raw = current.licenses
    if raw is not None:
        lic = LicenseSet(str(raw["holder"]), str(raw["text"]), str(raw["code"]), str(raw["data"]))
        asked = False
    else:
        holder = _ask_holder(ctx, lang, lang("init.license.default_holder", name=project_name(current, repo)))
        chosen = {cat: _ask_license(ctx, lang, cat, suggested(cat, visibility)) for cat in CATEGORIES}
        lic = LicenseSet(holder, chosen["text"], chosen["code"], chosen["data"])
        asked = True
    present = set(entries)
    texts: dict[str, bytes] = {}
    for cat in CATEGORIES:
        license_id = lic.of(cat)
        if (license_id in texts or license_id == ALL_RIGHTS_RESERVED or license_text(license_id) is not None
                or f"LICENSES/{license_id}.txt" in present):
            continue
        texts[license_id] = _ask_license_text(ctx, lang, license_id)
    return lic, texts, asked


def _init(ctx: OpContext, language: str | None, fmt: str | None, disclosure: str | None, individual: bool) -> Result:
    project = ctx.project()
    assert project is not None
    root = project.root
    git = ctx.git()
    guard_remote(ctx, git)
    repo = _origin_repo(ctx, git)
    tree = worktree(root, git=git)
    read, _exists = tree_reader(tree)
    current = _tree_project(read)
    base = str(current.base_branch)
    if fetch_base(git, base).offline:
        raise OpError("E_NET_OFFLINE")
    forge = ctx.forge()
    ident = forge.identity(refresh=True)
    caps = forge.capabilities(refresh=True)  # 08 REQ-FORGE-18
    if caps.default_branch != base:  # 06 REQ-PERM-24
        raise OpError("E_CONFIG_BASE_NOT_DEFAULT", details={"default_branch": caps.default_branch})
    if caps.viewer_permission != "admin":
        raise OpError("E_PERM_ROLE_REQUIRED", details={"login": ident.login, "required_role": ["lead"],
                                                       "permission": caps.viewer_permission})
    enforced = caps.profile == "enforced"
    accepting = not enforced and current.advisory_acceptance is None
    chosen_language = language or current.language
    lang = translator(chosen_language)  # type: ignore[arg-type]
    licenses, license_texts, asked = license_choices(ctx, current, repo, caps.visibility, lang, tree.entries().keys())
    choices = Choices(
        language=chosen_language,  # type: ignore[arg-type]
        format=fmt or current.format,
        disclosure=disclosure or current.disclosure,
        individual=individual,
        repo=repo,
        core_pin=_meta.__version__,
        lead=ident.login,
        # 06 REQ-PERM-41 (enmendado): la aceptación queda en project.yaml, dentro del commit de init;
        # se planea desde el principio y solo se escribe si la persona teclea la frase
        acceptance=(ident.login, _iso(ctx.inv.now())) if accepting else None,
        licenses=licenses,
        license_texts=license_texts,
        year=ctx.inv.now().year,
    )
    plan = plan_files(read, tree.entries().keys(), choices, t=lang)
    roles = plan.roles
    protected = caps.base_rules is not None and caps.base_rules.source != "none"
    if not same_login(roles.lead, ident.login):
        raise OpError("E_PERM_ROLE_REQUIRED", details={"login": ident.login, "required_role": ["lead"]})
    if protected and not same_login(ctx.base().roles.lead, ident.login):  # D-08: la política de la base
        raise OpError("E_PERM_ROLE_REQUIRED", details={"login": ident.login, "required_role": ["lead"]})
    setup = _setup_plan(forge, ident.login, read, plan)
    st = _Run(ctx, lang, repo)
    group = plan.project.mode != "individual"
    wants_bypass = enforced and group and roles.deputy is None
    if not enforced:
        st.warn("W_FORGE_PROFILE_ADVISORY")
    if caps.rules_available is None:
        st.warn("W_FORGE_CAPABILITY_UNKNOWN")
    if group and roles.deputy is None:
        st.warn("W_PERM_DEPUTY_MISSING")
    if plan.changes:
        _check_worktree(git, base, push=not protected)
        check_targets(root, [c.path for c in plan.changes if c.action != "deleted"], tree.entries())
    steps = Steps(ctx, OP, STEPS, deadline_s=None)  # 05 REQ-OPS-22: init no tiene plazo
    if steps.dry_run:
        return _dry(steps, st, plan, caps, protected, setup)
    ctx.confirm_human(OP, repo, _summary(st, plan, caps, protected, choices, new_licenses=asked, setup=setup))
    if not enforced:  # 06 REQ-PERM-40: sin la frase, nada se escribe
        if not ctx.type_phrase(lang("init.advisory.table"), lang("init.advisory.phrase")):
            raise OpError("E_FORGE_CAPABILITY_MISSING", details={"profile": "advisory", "reason": "not_accepted"})
        if accepting:
            st.pending.append(lang("init.pending.advisory_decision"))
    bypass = wants_bypass and ctx.type_phrase(lang("init.bypass.text", login=ident.login), f"bypass {ident.login}")
    if wants_bypass and not bypass:
        st.pending.append(lang("init.pending.bypass_declined"))
    token = ctx.ask_secret(lang("init.token.prompt"))
    admin = ctx.forge_admin(token)
    del token
    if not same_login(admin.identity().login, ident.login):
        raise OpError("E_AUTH_INSUFFICIENT_SCOPE", details={"reason": "admin_token_account"})
    write = _Writer()
    uses = template_actions(_workflows(read, tree.entries().keys(), plan.changes))
    steps.run("repo_settings", lambda: _repo_settings(st, admin, caps, write))
    steps.run("actions", lambda: (_actions(st, admin, caps, uses, write),
                                   _evidence_environment(st, admin, caps, base, plan.project.evidence_signing.mode,
                                                         write)))
    steps.run("collaborators", lambda: _collaborators(st, admin, roles, ident.login, write))
    steps.run("labels", lambda: _labels(st, forge, write))
    pushed = False
    if plan.changes:
        steps.run("files", lambda: _apply_files(root, plan.changes))
        if protected:
            steps.skip("push", "base_protected")
            st.warn("W_STATE_INIT_FILES_PENDING", {"paths": [c.path for c in plan.changes]})
            st.pending.append(lang("init.pending.files"))
        else:
            pushed = steps.run("push", lambda: _commit_push(git, base, plan.changes, lang))
    else:
        steps.skip("files", "unchanged")
        steps.skip("push", "unchanged")
    if enforced:
        steps.run("rulesets", lambda: _rulesets(st, admin, caps, plan, ident.login, bypass, write))
        steps.run("verify", lambda: _verify(admin, base, plan))
    else:
        for name in (BASE_RULESET, TASK_RULESET, WORKFLOWS_RULESET):
            st.protection(f"ruleset:{name}", "unsupported", "advisory")
        steps.skip("rulesets", "advisory")
        steps.skip("verify", "advisory")
    setup_number = steps.run("setup_task", lambda: _setup_task(st, forge, plan, setup, write))
    try:
        _org_report(st, admin)
    except ForgeError:  # solo informa (06 §4.10): una falla aquí no deshace lo aplicado
        pass
    for kept in plan.kept:
        st.pending.append(lang(f"init.pending.{kept.reason}", path=kept.path))
    data = {
        "repo": repo,
        "profile": caps.profile,
        "advisory_accepted": plan.project.advisory_acceptance is not None,
        "files": _files_data(plan.changes, pending=protected),
        "labels": st.labels,
        "protections": st.protections,
        "pushed": pushed,
        "setup_task": setup_number,
        "pending_steps": st.pending,
    }
    return ok(OP, data, [*st.warnings, *steps.warnings(ctx.t())])


def _check_worktree(git: Git, base: str, *, push: bool) -> None:
    """Árbol limpio y, si se va a subir a la base, en la base y al día con ``origin``."""
    if dirty(git):
        raise OpError("E_STATE_DIRTY_WORKTREE", details={"reason": "uncommitted_changes"})
    if not push:
        return
    if git.current_branch() != base:
        raise OpError("E_STATE_DIRTY_WORKTREE", details={"reason": "not_on_base", "branch": git.current_branch()})
    if git.rev_parse("HEAD") != git.rev_parse(f"refs/remotes/origin/{base}"):
        raise OpError("E_STATE_DIRTY_WORKTREE", details={"reason": "base_out_of_date"})


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _workflows(read: Callable[[str], bytes | None], entries: Iterable[str], changes: Sequence[Change]) -> dict[str, bytes]:
    """Los workflows tal como quedarán: los del árbol con los cambios del plan encima."""
    out: dict[str, bytes] = {}
    for rel in entries:
        if rel.startswith(".github/workflows/"):
            data = read(rel)
            if data is not None:
                out[rel] = data
    for change in changes:
        if change.path.startswith(".github/workflows/"):
            if change.content is None:
                out.pop(change.path, None)
            else:
                out[change.path] = change.content
    return out


def _summary(st: _Run, plan: FilePlan, caps: Capabilities, protected: bool, choices: Choices, *,
             new_licenses: bool = False, setup: _Setup | None = None) -> str:
    """El resumen de la fase 2: qué hará ``init`` y sobre qué (05 §10). Con una licencia abierta
    para ``text`` recién elegida, el aviso de 11-trazabilidad REQ-TRACE-63."""
    t = st.lang
    count = {a: sum(1 for c in plan.changes if c.action == a) for a in ("created", "updated", "deleted")}
    lines = [t("init.summary.head", repo=st.repo, profile=caps.profile)]
    lines.append(t("init.summary.config", language=choices.language, format=choices.format,
                   disclosure=choices.disclosure, mode=plan.project.mode))
    if plan.roles_completed:
        lines.append(t("init.summary.roles", lead=choices.lead))
    lic = choices.licenses
    if lic is not None:
        lines.append(t("init.license.summary", text=lic.text, code=lic.code, data=lic.data, holder=lic.holder))
        if new_licenses and is_open(lic.text):
            lines.append(t("init.license.open_text", id=lic.text))
    if plan.changes:
        where = t("init.summary.where_pending") if protected else t("init.summary.where_push", base=caps.default_branch)
        lines.append(t("init.summary.files", where=where, **count))
    else:
        lines.append(t("init.summary.no_files"))
    if plan.kept:
        lines.append(t("init.summary.kept", paths=", ".join(k.path for k in plan.kept)))
    lines.append(t("init.summary.settings"))
    lines.append(t("init.summary.rulesets") if caps.profile == "enforced" else t("init.summary.no_rulesets"))
    if setup is not None and setup.create:  # 00-marco D-70: la aceptación queda dentro de esta confirmación
        lines.append(t("init.summary.setup_task", title=t("init.setup_task.title")))
    lines.append(t("init.summary.token"))
    return "\n".join(lines)


def _dry(steps: Steps, st: _Run, plan: FilePlan, caps: Capabilities, protected: bool, setup: _Setup) -> Result:
    """``--dry-run`` (REQ-OPS-30): lo que haría, sin el token ni escrituras."""
    t = st.lang
    for change in plan.changes:
        steps.effect("file_write", change.path, t(f"init.effect.{change.action}", path=change.path))
    if plan.changes and not protected:
        steps.effect("git_commit", caps.default_branch, t("init.effect.commit", base=caps.default_branch))
        steps.effect("git_push", caps.default_branch, t("init.effect.push", base=caps.default_branch))
    steps.effect("forge_update", st.repo, t("init.effect.settings"))
    steps.effect("forge_update", st.repo, t("init.effect.labels"))
    if caps.profile == "enforced":
        steps.effect("forge_update", st.repo, t("init.effect.rulesets"))
    if setup.create:
        steps.effect("forge_create", "issue", t("init.effect.setup_task", title=t("init.setup_task.title")))
    return steps.dry_result(st.warnings)


# -- los pasos sobre la forja -----------------------------------------------------------------------


def _repo_settings(st: _Run, admin: ForgeAdmin, caps: Capabilities, write: _Writer) -> None:
    """06 §4.2 y 11 §3.2 (REQ-PERM-36, REQ-TRACE-35): también en ``advisory`` (REQ-PERM-42)."""
    changes = settings_changes(admin.repo_settings(), desired_settings(caps.visibility))
    if changes:
        write(lambda: admin.update_repo_settings(changes))
        st.protection("repo_settings", "applied", ",".join(changes))
    else:
        st.protection("repo_settings", "unchanged")


def _actions(st: _Run, admin: ForgeAdmin, caps: Capabilities, uses: Sequence[str], write: _Writer) -> None:
    """06 §4.3: la política (REQ-PERM-56), SHA fijado (REQ-PERM-51), el token de los workflows
    (REQ-PERM-27), la retención (REQ-PERM-57) y, en repos públicos, ``pull_request_target``."""
    repo_policy = admin.actions_policy("repo")
    org_policy = admin.actions_policy("org")
    restriction = actions_restriction(org_policy, repo_policy, uses, org_owned=caps.owner_type == "Organization")
    if restriction is not None:
        st.warn("W_FORGE_ACTIONS_RESTRICTED", restriction)
    if repo_policy is not None and repo_policy.enabled and repo_policy.sha_pinning_required is True:
        st.protection("actions_permissions", "unchanged")
    else:
        allowed = repo_policy.allowed_actions if repo_policy is not None and repo_policy.allowed_actions else "all"
        write(lambda: admin.set_actions_policy(enabled=True, allowed_actions=allowed, sha_pinning_required=True))
        st.protection("actions_permissions", "applied", "sha_pinning_required")
    perms = admin.workflow_permissions("repo")
    if perms is not None and perms.default_workflow_permissions == "read" and not perms.can_approve_pull_request_reviews:
        st.protection("workflow_permissions", "unchanged")
    else:
        write(lambda: admin.set_workflow_permissions(default="read", can_approve=False))
        st.protection("workflow_permissions", "applied")
    target = RETENTION_DAYS.get(caps.visibility, 90)
    current = admin.retention_days()
    if current is None:
        st.protection("retention", "unsupported")
        st.pending.append(st.lang("init.pending.retention", days=target))
    elif current >= target:
        st.protection("retention", "unchanged", str(current))
    else:
        try:
            write(lambda: admin.set_retention_days(target))
        except (ForgeUnsupported, ForgeValidationError):  # sin endpoint, o la org impone un máximo menor
            st.protection("retention", "unsupported", str(current))
            st.pending.append(st.lang("init.pending.retention", days=target))
        else:
            st.protection("retention", "applied", str(target))
    if caps.visibility == "public":  # 06 §4.3: el endpoint lo fija una prueba de contrato de 08
        st.protection("pull_request_target", "skipped")
        st.pending.append(st.lang("init.pending.pull_request_target"))


def _evidence_environment(st: _Run, admin: ForgeAdmin, caps: Capabilities, base: str, mode: str,
                          write: _Writer) -> None:
    """06 §4.3b (REQ-PERM-61; 00-marco D-72): la llave del proyecto nunca es un secreto del
    repositorio ni de la organización, y el environment ``<nombre>-evidence`` solo admite la rama
    base, sea cual sea ``evidence_signing.mode``: un workflow que lo nombra lo crearía sin reglas.
    Con ``mode: ci``, si al environment le falta el secreto, queda en ``pending_steps``: ``init``
    nunca escribe secretos."""
    scopes = ("repo", "org") if caps.owner_type == "Organization" else ("repo",)
    for scope in scopes:  # antes de crear el environment
        names = admin.secret_names(scope)  # type: ignore[arg-type]
        if names is None:
            st.pending.append(st.lang(f"init.pending.evidence_secrets_{scope}", secret=EVIDENCE_SECRET))
        elif EVIDENCE_SECRET in names:
            raise OpError("E_CONFIG_EVIDENCE_KEY", details={"reason": "repo_secret", "scope": scope})
    if environments_available(caps) is False:
        st.protection("environment", "unsupported", EVIDENCE_ENVIRONMENT)
        return
    current = admin.environment(EVIDENCE_ENVIRONMENT)
    if current is not None and current.only_branch(base):
        st.protection("environment", "unchanged", EVIDENCE_ENVIRONMENT)
    else:
        try:
            write(lambda: admin.put_environment(EVIDENCE_ENVIRONMENT, branch=base))
        except ForgeUnsupported:  # el plan no admite environments
            st.protection("environment", "unsupported", EVIDENCE_ENVIRONMENT)
            return
        st.protection("environment", "applied", EVIDENCE_ENVIRONMENT)
    if mode == "ci":
        secrets = admin.secret_names("environment", EVIDENCE_ENVIRONMENT)
        if secrets is None or EVIDENCE_SECRET not in secrets:
            st.pending.append(st.lang("init.pending.evidence_secret", secret=EVIDENCE_SECRET,
                                      environment=EVIDENCE_ENVIRONMENT))


def _collaborators(st: _Run, admin: ForgeAdmin, roles: Roles, lead: str, write: _Writer) -> None:
    """06 §4.4: escritura directa para cada persona activa (REQ-PERM-28); nunca quita a nadie (REQ-PERM-29)."""
    for person in roles.people:
        if not person.active or same_login(person.login, lead):
            continue
        role = admin.collaborator_permission(person.login)
        if role in _STAFF_ROLES:
            st.protection(f"collaborator:{person.login}", "unchanged", role)
            continue
        result = write(lambda login=person.login: admin.add_collaborator(login, "push"))
        st.protection(f"collaborator:{person.login}", "applied", result)
        if result == "invited":
            st.pending.append(st.lang("init.pending.invited", login=person.login))
    outside = admin.outside_collaborators()
    logins = sorted(p.login for p in roles.people if any(same_login(p.login, o) for o in outside))
    if logins:  # 06 REQ-PERM-55
        st.warn("W_AUTH_BROAD_TOKEN", {"reason": "outside_collaborator", "logins": logins})


def _labels(st: _Run, forge: ForgeAdapter, write: _Writer) -> None:
    """06 §4.5: las 12 etiquetas, con la descripción en el idioma del proyecto."""
    wanted = labels_for(st.lang)
    actions = write(lambda: forge.ensure_labels(wanted))
    st.labels = [{"name": lab.name, "action": actions.get(lab.name, "unchanged")} for lab in wanted]


def _commit_push(git: Git, base: str, changes: Sequence[Change], lang: Translator) -> bool:
    """El commit de ``init`` (``AI-Use: none``, 11 §2.5) y el push a la base, sin forzar."""
    paths = sorted({c.path for c in changes})
    git.run("--literal-pathspecs", "add", "-A", "--", *paths, read=False)
    text = commit_message(lang("init.commit"), trailer_block(task=None, sections=(), ai_use=("none",), assisted=()))
    proc = git.run("--literal-pathspecs", "commit", "--quiet", "--cleanup=whitespace", "-F", "-", "--", *paths,
                   input=text.encode("utf-8"), check=False, read=False)
    if proc.returncode != 0:
        raise OpError("E_CONFIG_GIT_UNAVAILABLE", details={"reason": "commit_failed"})
    result = push_ref(git, f"refs/heads/{base}")
    if result.outcome in ("accepted", "up_to_date"):
        return True
    if result.outcome == "offline":
        raise OpError("E_NET_OFFLINE")
    reason = "non_fast_forward" if result.outcome == "non_ff" else (result.reason or "rejected")
    raise OpError("E_FORGE_PUSH_REJECTED", details={"reason": reason, "ref": f"refs/heads/{base}"})


def _rulesets(st: _Run, admin: ForgeAdmin, caps: Capabilities, plan: FilePlan, lead: str, bypass: bool,
              write: _Writer) -> None:
    """06 §4.7 y §4.7b, como última protección (REQ-PERM-26); con ``mode: individual``, REQ-PERM-59."""
    individual = plan.project.mode == "individual"
    checks = required_checks(plan.project.format)
    lead_id = admin.user_id(lead) if (bypass or caps.push_rulesets) else None
    wanted = [
        (BASE_RULESET, base_ruleset(caps.default_branch, checks=checks, individual=individual,
                                    bypass_id=lead_id if bypass else None)),
        (TASK_RULESET, task_ruleset()),
    ]
    if caps.push_rulesets and lead_id is not None:
        wanted.append((WORKFLOWS_RULESET, workflows_ruleset(lead_id)))
    else:
        st.protection(f"ruleset:{WORKFLOWS_RULESET}", "unsupported", "push_rulesets")
        st.warn("W_FORGE_CHECKS_SPOOFABLE")
    for name, payload in wanted:
        current = admin.ruleset(name)
        if current is not None and covers(current.payload, payload):
            st.protection(f"ruleset:{name}", "unchanged")
            continue
        write(lambda name=name, payload=payload: admin.upsert_ruleset(name, payload))
        st.protection(f"ruleset:{name}", "applied")


def _verify(admin: ForgeAdmin, base: str, plan: FilePlan) -> None:
    """REQ-PERM-38: las reglas efectivas de la base tienen todo lo de §4.7."""
    missing = missing_rules(admin.branch_rules(base), checks=required_checks(plan.project.format),
                            individual=plan.project.mode == "individual")
    if missing:
        raise OpError("E_FORGE_CAPABILITY_MISSING", details={"reason": "rules_not_applied", "missing": missing})


def _org_report(st: _Run, admin: ForgeAdmin) -> None:
    """06 §4.10: los ajustes de la organización que difieren; init no los cambia."""
    settings = admin.org_settings() or {}
    changes = [f"{k}={_shown(v)}" for k, v in ORG_RECOMMENDED.items() if k in settings and settings[k] != v]
    perms = admin.workflow_permissions("org")
    if perms is not None and (perms.default_workflow_permissions != "read" or perms.can_approve_pull_request_reviews):
        changes.append("default_workflow_permissions=read, can_approve_pull_request_reviews=false")
    if changes:
        st.pending.append(st.lang("init.pending.org", changes="; ".join(changes)))


def _shown(value: object) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


# -- la tarea de preparación (00-marco D-70) ----------------------------------------------------------


@dataclass(frozen=True)
class _Setup:
    """La tarea de preparación que ya existe, o si hay que crearla."""

    existing: Issue | None
    create: bool


def _planned(rel: str, read: Callable[[str], bytes | None], changes: Sequence[Change]) -> bytes | None:
    """El contenido de ``rel`` tal como lo deja ``init``: el del plan de archivos, o el del árbol."""
    for change in changes:
        if change.path == rel:
            return change.content
    return read(rel)


def _setup_plan(forge: ForgeAdapter, me: str, read: Callable[[str], bytes | None], plan: FilePlan) -> _Setup:
    """05 §9.3: la del marcador ``op`` del actor; si no hay ninguna tarea y la preparación queda
    pendiente (REQ-OPS-69), hay que crearla. Con otras tareas y sin la suya, nada."""
    marker = f"<!-- {_meta.NAME}:op v1 id={SETUP_OP_ID} -->"
    issues = forge.list_issues(labels=[TASK_LABEL], state="all")
    own = [i for i in issues if same_login(i.author, me) and marker in i.body]
    if own:
        return _Setup(min(own, key=lambda i: i.number), create=False)
    if issues:
        return _Setup(None, create=False)
    pending = setup_pending(_planned(PLAN_FILE, read, plan.changes), plan.roles, plan.project.mode)
    return _Setup(None, create=pending)


def _setup_task(st: _Run, forge: ForgeAdapter, plan: FilePlan, setup: _Setup, write: _Writer) -> int | None:
    """Crea la tarea, con las partes del ``lead`` y en el idioma del proyecto, y la acepta (04 REQ-TASK-8).

    Una tarea encontrada que sigue con ``state:proposed`` (la ejecución anterior falló antes de
    quitarla) se acepta igual; una cerrada ya cumplió su función y no deja paso pendiente.
    """
    t = st.lang
    issue = setup.existing
    if issue is None:
        if not setup.create:
            return None
        spec = {
            "summary": t("init.setup_task.summary", name=_meta.NAME),
            # 04 P3: solo las suyas; justo después de init, todas
            "sections": [s.id for s in plan.roles.sections if same_login(s.owner, plan.roles.lead)],
            "plan_refs": [],
            "done": [t(f"init.setup_task.done_{key}", name=_meta.NAME) for key in SETUP_DONE],
        }
        body = render_task_body(spec, t.lang)
        title = t("init.setup_task.title")
        issue = write(lambda: forge.create_issue(title, body, labels=[TASK_LABEL, PROPOSED], op_id=SETUP_OP_ID))
    if issue.state != "open":
        return issue.number
    if any(label.casefold() == PROPOSED for label in issue.labels):
        number = issue.number
        write(lambda: forge.remove_label(number, PROPOSED))
    st.pending.insert(0, t("init.pending.setup_task", task=issue.number, title=issue.title))
    return issue.number


# -- vista legible ----------------------------------------------------------------------------------


def render_text(result: Result, t: Translator, *, fmt: str = "text") -> str:
    """Archivos, etiquetas, protecciones, pasos pendientes y los recordatorios de D-44 y D-49."""
    del fmt
    data = result.data or {}
    if data.get("dry_run"):
        return ""
    lines = [t("init.render.head", repo=data.get("repo"), profile=data.get("profile"))]
    files = data.get("files") or []
    if files:
        lines.append(t("init.render.files", count=len(files)))
        lines.extend(f"  {f.get('action')}: {f.get('path')}" for f in files)  # type: ignore[union-attr]
    labels = [lab for lab in data.get("labels") or [] if lab.get("action") != "unchanged"]  # type: ignore[union-attr]
    if labels:
        lines.append(t("init.render.labels", names=", ".join(str(lab.get("name")) for lab in labels)))  # type: ignore[union-attr]
    for prot in data.get("protections") or []:
        lines.append(f"  {prot.get('action')}: {prot.get('id')}")  # type: ignore[union-attr]
    if data.get("pushed"):
        lines.append(t("init.render.pushed"))
    pending = data.get("pending_steps") or []
    if pending:
        lines.append(t("init.render.pending"))
        lines.extend(f"  - {step}" for step in pending)  # type: ignore[union-attr]
    if data.get("labels"):  # init completo: D-44 y D-49
        lines.append(t("init.render.reminders"))
    return "\n".join(lines)
