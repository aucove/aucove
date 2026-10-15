# Guía de mantenimiento del repositorio del framework

Los ajustes de GitHub y de PyPI que el código no puede comprobar desde dentro del
repositorio. Una persona con permiso de administración los aplica al configurar el
repositorio y los revisa antes de cada release (punto 10). Cada título dice cuándo se
repite el punto.

Cada punto se marca solo con su evidencia: la salida del comando indicado (o una captura
de pantalla cuando no hay API), la fecha y quién lo comprobó. La evidencia se pega como
comentario en el Issue del release y se enlaza en la línea «Evidencia» del punto.

En los comandos, `OWNER/REPO` es el repositorio del framework y `ORG` la organización
dueña. Hace falta `gh` con sesión iniciada por una persona administradora.

## 0. Fijaciones verificadas (REQ-SEC-47): cada vez que cambia una y antes de cada release

- [ ] `python tools/verify_action_pins.py` sale con 0. Comprueba con `git ls-remote` que
  cada SHA de `.github/workflows/` y de `template/base/.github/workflows/` es el de la
  etiqueta de su comentario; lee el `action.yml` de cada acción en ese commit y rechaza
  una acción Docker cuya imagen no esté fijada por digest (un `FROM` con etiqueta, como
  el de `fsfe/reuse-action`), y con la API del registro comprueba que existen los
  digests de la imagen TeX (`build.image` de `template/base/project.yaml`) y de la de
  REUSE (`REUSE_IMAGE` del job `reuse` de `ci.yml`).
- [ ] La versión de uv de `ci.yml` (`version:` de `astral-sh/setup-uv`, la misma en todos
  los jobs) tiene su sha256 en `src/download/checksum/known-checksums.json` del SHA fijado
  de `astral-sh/setup-uv`, para `x86_64-unknown-linux-gnu`, `x86_64-pc-windows-msvc` y
  `aarch64-apple-darwin`. Si no lo tiene, setup-uv solo la contrasta con su manifiesto
  remoto. Dependabot no cambia ese `version:`: se sube a mano junto con la acción.
- [ ] La versión y el sha256 de gitleaks están verificados contra
  `gitleaks_<versión>_checksums.txt` de esa versión, y son iguales en
  `tools/template_vars.toml` y en `ci.yml` (job `test`).
- [ ] El sha256 de actionlint de `ci.yml` (job `lint`) coincide con
  `actionlint_<versión>_checksums.txt` de esa versión.
- [ ] `LICENSES/` tiene los textos de SPDX y `reuse lint` pasa en local sobre el repo y
  sobre los renders en y es (`python tools/render_template.py --lang en --out <dir>` y
  `reuse --root <dir> lint`).
- Evidencia: salida de `python tools/verify_action_pins.py`, de los dos `reuse lint` y de
  `curl -fsSL https://raw.githubusercontent.com/astral-sh/setup-uv/<SHA>/src/download/checksum/known-checksums.json | grep -- '-<versión de uv>"'`.

## 1. Repositorio: una vez

- [ ] El repositorio es público, de la organización del framework, y su rama por defecto
  es `main`. No vive en la organización de pruebas (00-marco D-24) ni en la del piloto
  (D-26).
- [ ] La wiki está desactivada: su contenido se editaría sin PR ni revisión.
- Comando: `gh api repos/OWNER/REPO --jq '{full_name, visibility, owner: .owner.login, default_branch, has_wiki}'`
- Evidencia:

## 2. Mantenedores con 2FA (REQ-SEC-79): una vez

- [ ] La organización exige la verificación en dos pasos a todos sus miembros
  (Settings > Authentication security).
- Comando: `gh api orgs/ORG --jq .two_factor_requirement_enabled` (debe dar `true`; solo
  lo ve un owner).
- Evidencia:

## 3. Sign-off obligatorio en los commits web (REQ-TRACE-66): una vez

- [ ] Activado *Require contributors to sign off on web-based commits* (Settings >
  General), en el repositorio y en la organización.
- Comando: `gh api repos/OWNER/REPO --jq .web_commit_signoff_required` y
  `gh api orgs/ORG --jq .web_commit_signoff_required` (los dos deben dar `true`).
- Evidencia:

## 4. Acciones fijadas por SHA completo (REQ-SEC-48): una vez

- [ ] Activado *Require actions to be pinned to a full-length commit SHA* en la
  organización (09-ci REQ-CI-5).
- Comando: `gh api orgs/ORG/actions/permissions --jq '{allowed_actions, sha_pinning_required}'`
  (`sha_pinning_required` debe dar `true`).
- Evidencia:

## 5. Acciones permitidas por la política (00-marco D-39): cada vez que se agrega una acción

- [ ] La política de Actions del repositorio y de la organización permite las acciones
  que usan los workflows del framework y la plantilla: las creadas por GitHub
  (`actions/checkout`, `actions/upload-artifact`, `actions/download-artifact`,
  `actions/attest` y `actions/create-github-app-token`), `astral-sh/setup-uv` y, solo para
  `release.yml`, `pypa/gh-action-pypi-publish`. No vale `allowed_actions: local_only`, ni
  `selected` sin ellas.
  No se usa `fsfe/reuse-action`: el job `reuse` corre la imagen `REUSE_IMAGE` con
  `docker run`, fijada por digest. La lista exacta sale de
  `python tools/verify_action_pins.py --list`.
- Comando: `gh api repos/OWNER/REPO/actions/permissions --jq '{enabled, allowed_actions}'`;
  si `allowed_actions` es `selected`,
  `gh api repos/OWNER/REPO/actions/permissions/selected-actions --jq '{github_owned_allowed, patterns_allowed}'`
  (`github_owned_allowed: true` y `patterns_allowed` con `astral-sh/setup-uv@*` y
  `pypa/gh-action-pypi-publish@*`). Lo mismo con `orgs/ORG/actions/permissions`.
- Evidencia:

## 6. Dependabot activo (REQ-SEC-51): una vez y cada vez que cambia `dependabot.yml`

- [ ] Alertas de Dependabot y actualizaciones de seguridad activadas (Settings > Advanced
  Security).
- [ ] `.github/dependabot.yml` se procesa sin errores para `github-actions` (`/` y
  `/template/base/.github/workflows`, un PR por acción para los dos directorios) y `uv`
  (`/`), con `cooldown.default-days: 7`.
- Un PR de una acción falla hasta que un commit sobre su rama actualiza la copia de 09-ci y
  los pines de `tests/render/` (`EXPECTED_PINS` y `PINS`), y `tools/verify_action_pins.py`
  sale con 0 (punto 0). Un PR de uv que sube un piso de `pyproject.toml` se cierra: los
  pisos son los de 01-arquitectura §4.
- Comando: `gh api -i repos/OWNER/REPO/vulnerability-alerts` (debe dar `204`) y
  `gh api repos/OWNER/REPO/automated-security-fixes --jq .enabled` (debe dar `true`).
  Para la configuración: captura de Insights > Dependency graph > Dependabot.
- Evidencia:

## 7. Reporte privado de vulnerabilidades (REQ-SEC-77): una vez

- [ ] Activado *Private vulnerability reporting* (Settings > Advanced Security), que es el
  canal de [SECURITY.md](../SECURITY.md).
- Comando: `gh api repos/OWNER/REPO/private-vulnerability-reporting --jq .enabled` (debe
  dar `true`).
- Evidencia:

## 8. Environment `pypi` y proyecto en PyPI (REQ-SEC-40 y REQ-SEC-79): una vez y al cambiar los mantenedores

- [ ] El environment `pypi` tiene *Required reviewers* y no admite el bypass de
  administradores (*Allow administrators to bypass configured protection rules*
  desmarcado). Con dos o más mantenedores, el revisor es al menos uno distinto de quien lanza
  el release, con *Prevent self-review* activado. Con uno solo, el revisor es esa persona,
  sin *Prevent self-review* (00-marco D-71).
- [ ] Sus *Deployment branches and tags* admiten solo las etiquetas `v*`: `release.yml` corre
  con la etiqueta del release.
- [ ] En PyPI, el único publicador del proyecto es el Trusted Publisher de `OWNER/REPO`,
  con el workflow `release.yml` y el environment `pypi`. No hay tokens de API de PyPI
  guardados en GitHub.
- [ ] El ruleset de etiquetas `release-tags` (patrón `v*`, con *Restrict creations*,
  *Restrict updates* y *Restrict deletions*, y solo el rol *Repository admin* en la lista de
  bypass) impide que alguien sin administración cree o mueva una etiqueta de versión
  (00-marco D-71).
- [ ] Desde la primera publicación, cada mantenedor es colaborador del proyecto en PyPI, con
  2FA. Con dos o más mantenedores, son al menos dos.
- Comando: `gh api repos/OWNER/REPO/environments/pypi --jq '{name, can_admins_bypass, protection_rules, deployment_branch_policy}'`
  (`can_admins_bypass: false`, una regla `required_reviewers` con `prevent_self_review` en
  `true` desde que haya dos mantenedores, y una de ramas o etiquetas),
  `gh api repos/OWNER/REPO/rulesets --jq '.[] | {name, target, enforcement}'` y
  `gh secret list --repo OWNER/REPO` (sin tokens de PyPI). En PyPI: captura de
  *Publishing* y de *Collaborators* del proyecto.
- Evidencia:

## 9. Environment `forge-tests` (12-mvp §3.1 y §3.3): una vez y en cada rotación de tokens

- [ ] El environment `forge-tests` tiene los secretos de las pruebas contra GitHub real:
  `TEST_GH_TOKEN_LEAD`, `TEST_GH_TOKEN_RESEARCHER_A`, `TEST_GH_TOKEN_READ_ONLY` y
  `TEST_GH_TOKEN_NO_PERMISSION`; `TEST_GH_TOKEN_RESEARCHER_B` y `TEST_GH_TOKEN_TEAM_LEAD`
  solo si existen esas cuentas (12-mvp REQ-MVP-28; 00-marco D-24).
- [ ] Los tokens son de grano fino, limitados a los repositorios sandbox de la
  organización de pruebas, con caducidad de 90 días o menos y la rotación agendada.
- [ ] Tiene las variables `CONTRACT_REPO_PUBLIC` y `CONTRACT_REPO_PRIVATE`, con los dos
  repositorios sandbox (`<org de pruebas>/ct-public` y `<org de pruebas>/ct-private`), que
  `nightly.yml` pasa como `AUCOVE_CONTRACT_REPO` (08-forja §10.2).
- [ ] Sus *Deployment branches and tags* admiten `main` y las etiquetas `v*`: `nightly.yml`
  corre cada noche en `main` y `release.yml` lo llama antes de publicar (REQ-FORGE-64; 12-mvp
  REQ-MVP-22). No lleva revisores, porque la corrida nocturna no tiene a nadie que la apruebe.
- Comando: `gh secret list --env forge-tests --repo OWNER/REPO` (muestra nombres y fechas,
  nunca valores), `gh variable list --env forge-tests --repo OWNER/REPO` y
  `gh api repos/OWNER/REPO/environments/forge-tests --jq .deployment_branch_policy`.
- Evidencia:

## 10. Antes de cada release

- [ ] Hay un Issue del release donde se reúne la evidencia de esta guía.
- [ ] En `main`, `ci.yml` está en verde con la matriz completa (00-marco D-38 p), y también
  la última corrida nocturna de las pruebas contra GitHub real (08-forja REQ-FORGE-64).
- [ ] La versión está en `src/aucove/_meta.py` y en `tools/template_vars.toml` (`core_pin`,
  y `core` derivado con REQ-ARCH-44). `release.yml` falla si la etiqueta no es `v` más esa
  versión (`tools/release.py check-tag`).
- [ ] `CHANGELOG.md` tiene la sección `## [X.Y.Z] - AAAA-MM-DD`, porque las notas de la GitHub
  Release salen de ahí (`tools/release.py notes` falla sin ella). Entre versiones, los cambios se
  anotan en `## [Unreleased]`, y al preparar el release esa sección toma el número y la fecha.
- [ ] El README y la guía de uso (`docs/`) instalan y verifican esa versión exacta
  (01-arquitectura REQ-ARCH-30; lo comprueba `tests/unit/test_readme_version.py`), y los avisos
  de `docs/` sobre lo que una versión preliminar todavía no trae siguen siendo ciertos.
- [ ] Un mantenedor crea en `main` la etiqueta anotada (`git tag -a vX.Y.Z -m "aucove vX.Y.Z"`)
  y la sube, y otro aprueba el despliegue al environment `pypi` (REQ-SEC-79). Con un solo
  mantenedor, lo aprueba él mismo después de revisar que la corrida es la de su etiqueta y que
  el build terminó bien (00-marco D-71). Con una versión final, el de `template-publish` se
  aprueba igual (REQ-SEC-83).
- Comando: `gh run list --repo OWNER/REPO --workflow ci.yml --branch main --limit 1 --json databaseId,conclusion,headSha,url`,
  después `gh run view <id> --repo OWNER/REPO --json jobs --jq '.jobs[] | {name, conclusion}'`.
- Evidencia:

## 11. Primera publicación: una vez

- [ ] Antes de publicar, el nombre estaba disponible en PyPI, npm y GHCR y en al menos una
  base de marcas, y el resultado quedó registrado (11-trazabilidad REQ-TRACE-70; 00-marco
  D-03).
- [ ] El primer commit del repositorio lo hizo una persona con `git commit -s`, sin
  `Signed-off-by` ni `Co-authored-by` de una IA (11-trazabilidad REQ-TRACE-67).
- Comando: `git log --reverse --format='%H%n%B' | head -n 12`, en un clon del repositorio.
- Evidencia:

## 12. Repositorio plantilla: desde la primera versión final

- [ ] Existe `OWNER/aucove-template`, marcado como plantilla (`is_template: true`,
  01-arquitectura REQ-ARCH-9), con rama por defecto `main`.
- [ ] La GitHub App del job `template` está instalada solo en `aucove-template`, con
  `contents: write` y `workflows: write` (REQ-ARCH-10), y sus credenciales viven en el
  environment `template-publish`: el secreto `TEMPLATE_APP_PRIVATE_KEY` y la variable
  `TEMPLATE_APP_CLIENT_ID`.
- [ ] El environment `template-publish` tiene *Required reviewers*, sin el bypass de
  administradores, y solo las etiquetas `v*` en *Deployment branches and tags*. Con dos o más
  mantenedores, también *Prevent self-review* (10-seguridad REQ-SEC-83; 00-marco D-71).
- Comando: `gh api repos/OWNER/aucove-template --jq '{full_name, is_template, default_branch}'`
  y `gh api repos/OWNER/REPO/environments/template-publish --jq '{name, protection_rules, deployment_branch_policy}'`.
- Evidencia:
