# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Configuración de GitHub que aplica ``init`` y revisa ``doctor`` (06-permisos §4 y §5).

Piezas puras, sin llamadas a la forja: ``init`` las usa para escribir (lee, compara y escribe
solo si difiere, REQ-PERM-25) y ``doctor``, para informar la deriva (REQ-PERM-39). Así los
dos comparan contra lo mismo.

- Ajustes del repositorio (§4.2; 11-trazabilidad §3.2): ``desired_settings`` y
  ``settings_changes``.
- Acciones de los workflows y la política de Actions (§4.3, REQ-PERM-56):
  ``template_actions`` y ``actions_restriction``.
- Etiquetas (§4.5): ``LABELS`` y ``labels_for``.
- Rulesets (§4.7 y §4.7b; REQ-PERM-59 con ``mode: individual``): ``base_ruleset``,
  ``task_ruleset``, ``workflows_ruleset``, ``covers`` y ``missing_rules`` (sobre las reglas
  activas de la base, REQ-PERM-38). ``base_rules_missing`` hace la misma comparación sobre el
  resumen ``BaseRules`` de la detección de capacidades (08 §3.1), que es lo que lee ``doctor``.
- Ajustes de la organización (§4.10): ``ORG_RECOMMENDED``.
- El environment de la firma del proyecto (§4.3b; 00-marco D-72): ``EVIDENCE_ENVIRONMENT``,
  ``EVIDENCE_SECRET`` y ``environments_available``.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from .. import _meta
from ..domain.checks import GITHUB_ACTIONS_APP_ID
from ..forge.base import REPO_SETTINGS, ActionsPolicy, Label, rules_summary

if TYPE_CHECKING:
    from ..forge.base import BaseRules, Capabilities
    from ..i18n import Translator

__all__ = [
    "BASE_RULESET",
    "EVIDENCE_ENVIRONMENT",
    "EVIDENCE_SECRET",
    "LABELS",
    "ORG_RECOMMENDED",
    "TASK_RULESET",
    "WORKFLOWS_RULESET",
    "actions_restriction",
    "base_rules_missing",
    "base_ruleset",
    "covers",
    "desired_settings",
    "environments_available",
    "labels_for",
    "missing_rules",
    "settings_changes",
    "task_ruleset",
    "template_actions",
    "workflows_ruleset",
]

BASE_RULESET = f"{_meta.NAME}-base"
TASK_RULESET = f"{_meta.NAME}-task-branches"
WORKFLOWS_RULESET = f"{_meta.NAME}-workflows"
# 06 §4.3b (00-marco D-72): el environment de la firma del proyecto y el secreto con la llave privada.
EVIDENCE_ENVIRONMENT = f"{_meta.NAME}-evidence"
EVIDENCE_SECRET = f"{_meta.ENV_PREFIX}EVIDENCE_KEY"


def environments_available(caps: Capabilities) -> bool | None:
    """El repositorio admite environments: es público, o privado con Pro, Team o Enterprise (los
    mismos casos que el perfil ``enforced``, 06 REQ-PERM-61). ``None`` si la detección no fue
    concluyente (08 §3.2)."""
    if caps.visibility == "public":
        return True
    return caps.rules_available


# 06-permisos §4.10: lo que conviene en la organización (init solo lo informa).
ORG_RECOMMENDED: Mapping[str, object] = {
    "default_repository_permission": "none",
    "members_can_fork_private_repositories": False,
    "members_can_change_repo_visibility": False,
    "members_can_delete_repositories": False,
    "members_can_create_public_repositories": False,
    "two_factor_requirement_enabled": True,
}
# Etiquetas de 00-marco §5.4 sin las reservadas (06-permisos §4.5): nombre y color. La descripción,
# localizada según project.yaml: language (D-02), está en el catálogo (init.label.<nombre>, con «_»
# en lugar de «:»).
LABELS: tuple[tuple[str, str], ...] = (
    ("task", "1d76db"),
    ("direction", "c5def5"),
    ("state:proposed", "fbca04"),
    ("state:expired", "bfbfbf"),
    ("flag:overlap", "d93f0b"),
    ("flag:direction-change", "5319e7"),
    ("flag:blocked", "b60205"),
    ("flag:instructions", "d4c5f9"),
    ("ack:direction", "0e8a16"),
    ("ack:overlap", "c2e0c6"),
    ("main-broken", "b60205"),
    ("flag:bypass", "e99695"),
)
_USES = re.compile(r"^\s*(?:-\s*)?uses:\s*['\"]?([^\s'\"#]+)")


def labels_for(t: Translator) -> list[Label]:
    """Las etiquetas de 06 §4.5 con la descripción en el idioma de ``t``, el del proyecto (D-02)."""
    return [Label(name, color, t(f"init.label.{name.replace(':', '_')}")) for name, color in LABELS]


def desired_settings(visibility: str) -> dict[str, object]:
    """Ajustes del repositorio de 06 §4.2 (``allow_forking`` solo en repos privados)."""
    out: dict[str, object] = {
        "allow_squash_merge": True,
        "allow_merge_commit": False,
        "allow_rebase_merge": False,
        "squash_merge_commit_title": "PR_TITLE",
        "squash_merge_commit_message": "PR_BODY",
        "allow_auto_merge": False,
        "allow_update_branch": False,
        "delete_branch_on_merge": True,
        "has_wiki": False,
    }
    if visibility != "public":
        out["allow_forking"] = False
    return out


def settings_changes(current: Mapping[str, object], desired: Mapping[str, object]) -> dict[str, object]:
    """Las claves que difieren; título y mensaje del squash viajan juntos (GitHub lo exige)."""
    changes = {k: v for k, v in desired.items() if current.get(k) != v}
    squash = ("squash_merge_commit_title", "squash_merge_commit_message")
    if any(k in changes for k in squash):
        changes.update({k: desired[k] for k in squash if k in desired})
    return {k: changes[k] for k in REPO_SETTINGS if k in changes}


def template_actions(files: Mapping[str, bytes]) -> list[str]:
    """Las acciones (``owner/repo@sha``) que usan los workflows; sin los reutilizables locales."""
    found: set[str] = set()
    for rel, data in files.items():
        if not rel.startswith(".github/workflows/") or not rel.endswith((".yml", ".yaml")):
            continue
        for line in data.decode("utf-8", "replace").splitlines():
            match = _USES.match(line)
            if match is not None and not match.group(1).startswith(("./", "docker://")):
                found.add(match.group(1))
    return sorted(found)


def _allowed(policy: ActionsPolicy, use: str) -> bool:
    if not policy.enabled:
        return False
    if policy.allowed_actions == "all":
        return True
    if policy.allowed_actions != "selected":
        return False  # local_only o desconocida
    name = use.split("@", 1)[0]
    if name.split("/", 1)[0].lower() in ("actions", "github") and policy.github_owned_allowed:
        return True
    return any(fnmatch.fnmatchcase(use, p) or fnmatch.fnmatchcase(name, p) for p in policy.patterns_allowed or ())


def actions_restriction(org: ActionsPolicy | None, repo: ActionsPolicy | None, uses: Sequence[str],
                        *, org_owned: bool) -> dict[str, object] | None:
    """``details`` de ``W_FORGE_ACTIONS_RESTRICTED`` (REQ-PERM-56), o ``None`` si todo se permite.

    Primero la organización y después el repositorio. Si una lectura falló y la otra no muestra
    la restricción, el resultado es desconocido: ``level`` nulo y todas las acciones en ``missing``.
    """
    for level, policy in (("org", org), ("repo", repo)):
        if policy is None:
            continue
        missing = [u for u in uses if not _allowed(policy, u)]
        if missing:
            return {"level": level, "allowed_actions": policy.allowed_actions, "missing": missing}
    if repo is None or (org_owned and org is None):
        return {"level": None, "allowed_actions": "unknown", "missing": list(uses)}
    return None


def base_ruleset(base: str, *, checks: Sequence[str], individual: bool, bypass_id: int | None) -> dict[str, Any]:
    """Ruleset de la rama base (06 §4.7; REQ-PERM-59 con ``mode: individual``)."""
    review: dict[str, object] = {
        "required_approving_review_count": 0 if individual else 1,
        "dismiss_stale_reviews_on_push": True,
        "require_code_owner_review": not individual,
        "require_last_push_approval": not individual,
        "required_review_thread_resolution": False,
        "allowed_merge_methods": ["squash"],
    }
    bypass = [] if bypass_id is None else [{"actor_id": bypass_id, "actor_type": "User", "bypass_mode": "pull_request"}]
    return {
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": [f"refs/heads/{base}"], "exclude": []}},
        "bypass_actors": bypass,
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "pull_request", "parameters": review},
            {"type": "required_status_checks", "parameters": {
                "strict_required_status_checks_policy": False,
                "do_not_enforce_on_create": False,
                "required_status_checks": [{"context": c, "integration_id": GITHUB_ACTIONS_APP_ID} for c in checks],
            }},
        ],
    }


def task_ruleset() -> dict[str, Any]:
    """Ruleset de las ramas de tarea (06 REQ-PERM-52)."""
    return {
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["refs/heads/task/*"], "exclude": []}},
        "bypass_actors": [],
        "rules": [{"type": "non_fast_forward"}],
    }


def workflows_ruleset(lead_id: int) -> dict[str, Any]:
    """Push ruleset de los workflows (06 REQ-PERM-53), con el ``lead`` como única excepción."""
    return {
        "target": "push",
        "enforcement": "active",
        "bypass_actors": [{"actor_id": lead_id, "actor_type": "User", "bypass_mode": "always"}],
        "rules": [{"type": "file_path_restriction", "parameters": {"restricted_file_paths": [".github/workflows/**"]}}],
    }


def covers(current: object, desired: object) -> bool:
    """``True`` si ``current`` ya tiene todo lo de ``desired`` (la forja agrega campos propios).

    Los diccionarios se comparan por las claves de ``desired``; las listas, por largo y elemento a
    elemento sin importar el orden; lo demás, por igualdad.
    """
    if isinstance(desired, Mapping):
        return isinstance(current, Mapping) and all(k in current and covers(current[k], v) for k, v in desired.items())
    if isinstance(desired, list):
        if not isinstance(current, list) or len(current) != len(desired):
            return False
        pending = list(current)
        for item in desired:
            match = next((i for i, have in enumerate(pending) if covers(have, item)), None)
            if match is None:
                return False
            pending.pop(match)
        return True
    return bool(current == desired)


def missing_rules(rules: Sequence[Mapping[str, object]], *, checks: Sequence[str], individual: bool) -> list[str]:
    """Lo que falta de 06 §4.7 en las reglas activas de la base (REQ-PERM-38)."""
    types = {r.get("type") for r in rules}
    missing = [t for t in ("deletion", "non_fast_forward", "pull_request", "required_status_checks") if t not in types]
    summary = rules_summary(rules)
    missing += [f"check:{c}" for c in checks if c not in (summary.required_checks or ())]
    if "pull_request" in types and not individual:
        if (summary.required_approvals or 0) < 1:
            missing.append("required_approving_review_count")
        if not summary.require_code_owner_review:
            missing.append("require_code_owner_review")
        if not summary.require_last_push_approval:
            missing.append("require_last_push_approval")
    return missing


def base_rules_missing(rules: BaseRules, *, checks: Sequence[str], individual: bool) -> list[str]:
    """Lo que falta de 06 §4.7 en el resumen de las reglas de la base (08 §3.1).

    Mismos nombres que ``missing_rules``. El resumen no dice si hay regla ``deletion`` ni si hay
    regla ``pull_request`` sin aprobaciones: esas solo las ve ``missing_rules``. Con protección
    clásica, lo que GitHub no informa (``None``) no cuenta como faltante.
    """
    missing: list[str] = []
    if rules.blocks_force_push is False:
        missing.append("non_fast_forward")
    if rules.required_checks is not None:
        missing += [f"check:{c}" for c in checks if c not in rules.required_checks]
    if not individual:
        if rules.required_approvals is not None and rules.required_approvals < 1:
            missing.append("required_approving_review_count")
        if rules.require_code_owner_review is False:
            missing.append("require_code_owner_review")
        if rules.require_last_push_approval is False:
            missing.append("require_last_push_approval")
    return missing
