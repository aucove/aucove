# 03 — Esquemas de archivos y formularios

- **Código:** `SCHEMA`
- **Estado:** v0.1
- **Archivos normativos de este capítulo:** `schemas/project.schema.json`, `schemas/roles.schema.json`, `schemas/decision.schema.json`, `schemas/session-note.schema.json`, `schemas/pr-meta.schema.json` (JSON Schema draft 2020-12).

## 1. Alcance y autoridad

Este capítulo es la autoridad sobre el **formato** de siete artefactos (00-marco §6): `project.yaml`, `roles.yaml`, la sintaxis de `plan.md`, el registro de decisión, la nota de sesión, el formulario de Issue de tarea y la plantilla de PR. Define qué es válido, cómo se analiza cada archivo y qué problema se informa si algo falla.

Solo referencia, sin redefinirlos: el árbol y las clases de ruta (02-convencion); estados, reclamo y cambio de dirección (04-tareas); operaciones, sobre de resultado y catálogo consolidado de errores (05-operaciones); autorización y CODEOWNERS (06-permisos); hooks y paquete de contexto (07-hooks); API de GitHub (08-forja); workflows y checks, incluida la inmutabilidad de decisiones (09-ci); uso de `account_type` (10-seguridad); trailers, vocabulario de uso de IA, CRediT y libro de aprobaciones (11-trazabilidad).

| Artefacto | Ruta | Esquema (`$id`) | Clase (00-marco §5.2) |
|---|---|---|---|
| Configuración | `project.yaml` | `https://aucove.invalid/schemas/project/v1` | `lead` |
| Roles | `roles.yaml` | `https://aucove.invalid/schemas/roles/v1` | `lead` |
| Plan | `plan.md` | gramática en §5 | `lead` |
| Decisión | `memory/decisions/<YYYY-MM-DD>-<n>-<slug>.md` | `https://aucove.invalid/schemas/decision/v1` (front matter) | `append` |
| Nota de sesión | `memory/notes/task-<n>.md` | `https://aucove.invalid/schemas/session-note/v1` (front matter) | `task` |
| Formulario de tarea | `.github/ISSUE_TEMPLATE/task.yml` | sintaxis de issue forms de GitHub, §8 | `generated` |
| Plantilla de PR | `.github/pull_request_template.md` | `https://aucove.invalid/schemas/pr-meta/v1` (bloque del cuerpo, §9.3) | `lead` |

## 2. Reglas comunes

### 2.1 Codificación y YAML

- **REQ-SCHEMA-1.** Todo archivo de este capítulo DEBE estar en UTF-8; el núcleo DEBE descartar un BOM inicial y normalizar `\r\n` a `\n` antes de analizarlo.
- **REQ-SCHEMA-2.** El cargador YAML DEBE ser seguro (sin etiquetas `!!python` ni otras locales) y DEBE rechazar claves duplicadas, alias o anclas y claves que no sean cadenas.
- **REQ-SCHEMA-3.** El cargador NO DEBE convertir fechas en objetos fecha ni `yes`/`no`/`on`/`off` en booleanos: solo `true` y `false` (en cualquier capitalización simple) son booleanos, y `2026-10-01` queda como cadena. Los enteros solo se leen en decimal y los reales solo en notación decimal: `014` (octal), `0x1F`, `0b101`, `1_000`, `1:30` (sexagesimal), `.inf` y `.nan` quedan como cadenas, y el esquema los rechaza si espera un número. Una etiqueta explícita `!!bool`, `!!int` o `!!float` (también vía `%TAG`) con un valor fuera de esas formas, las etiquetas `!!binary`, `!!set`, `!!omap` y `!!pairs`, las etiquetas locales y la clave de fusión `<<` DEBEN dar un error de sintaxis (REQ-SCHEMA-4); `!!timestamp` explícito queda como cadena.
- **REQ-SCHEMA-89.** Un escalar sin comillas que se lea como número donde el esquema exige cadena DEBE producir `E_VALIDATION_SCHEMA` con el `hint` "escríbelo entre comillas"; el validador NUNCA lo convierte.
- **REQ-SCHEMA-4.** Un error de sintaxis YAML DEBE informarse con `E_CONFIG_YAML` en `project.yaml` y `roles.yaml`, y con `E_VALIDATION_FRONT_MATTER` en un front matter.

Cargador de referencia (PyYAML ≥ 6):

```python
import re, yaml

T = "tag:yaml.org,2002:"
BOOL = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)\Z")
INT = re.compile(r"^[-+]?(?:0|[1-9][0-9]*)\Z")                                  # solo decimal
FLOAT = re.compile(r"^[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+][0-9]+)?\Z")   # sin .inf ni .nan

class StrictLoader(yaml.SafeLoader):
    pass

def _error(msg, mark):
    return yaml.constructor.ConstructorError(None, None, msg, mark)

# Sin timestamps ni los booleanos, enteros y reales de YAML 1.1 (yes, 014, 1:30, 0x1F, 1_000...).
StrictLoader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v
        if tag not in (T + "timestamp", T + "bool", T + "int", T + "float", T + "value")]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
StrictLoader.add_implicit_resolver(T + "bool", BOOL, list("tTfF"))
StrictLoader.add_implicit_resolver(T + "int", INT, list("-+0123456789"))
StrictLoader.add_implicit_resolver(T + "float", FLOAT, list("-+0123456789."))

def _compose_node(self, parent, index):          # sin alias ni anclas
    event = self.peek_event()
    if self.check_event(yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
        raise _error("alias o ancla YAML no permitidos", event.start_mark)
    return yaml.composer.Composer.compose_node(self, parent, index)
StrictLoader.compose_node = _compose_node

def _mapping(loader, node, deep=False):          # sin claves duplicadas ni claves que no sean texto
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str) or key in seen:
            raise _error(f"clave inválida o duplicada: {key!r}", key_node.start_mark)
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)
StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)

def _reject(loader, node):                       # !!binary, !!set, !!omap, !!pairs, '<<' y etiquetas locales
    raise _error(f"etiqueta no permitida: {node.tag}", node.start_mark)
for tag in ("binary", "set", "omap", "pairs"):
    StrictLoader.add_constructor(T + tag, _reject)
StrictLoader.add_constructor(None, _reject)
StrictLoader.add_constructor(T + "timestamp", lambda loader, node: loader.construct_scalar(node))

def _typed(patterns, construct):                 # !!bool, !!int y !!float explícitos (también vía %TAG)
    def f(loader, node):
        value = loader.construct_scalar(node)
        if not any(rx.match(value) for rx in patterns):
            raise _error(f"valor no permitido para {node.tag}: {value!r}", node.start_mark)
        return construct(loader, node)
    return f
S = yaml.constructor.SafeConstructor
StrictLoader.add_constructor(T + "bool", _typed((BOOL,), S.construct_yaml_bool))
StrictLoader.add_constructor(T + "int", _typed((INT,), S.construct_yaml_int))
StrictLoader.add_constructor(T + "float", _typed((FLOAT, INT), S.construct_yaml_float))
```

### 2.2 Front matter

- **REQ-SCHEMA-5.** Un archivo con front matter DEBE empezar exactamente con la línea `---`; el front matter termina en la siguiente línea que sea `---` y DEBE ser un mapa YAML. Si falta o no es un mapa: `E_VALIDATION_FRONT_MATTER`.

### 2.3 Validación con JSON Schema

- **REQ-SCHEMA-6.** El núcleo DEBE validar con un validador draft 2020-12 con aserción de `format` activada, porque en 2020-12 `format` es solo anotación por defecto (en Python: `Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)`). El paquete DEBE depender de `jsonschema[format-nongpl]` (incluye `rfc3339-validator`; sin dependencias GPL, REQ-TRACE-59) y una prueba DEBE verificar que `'no-es-fecha'` falla contra `{"format": "date-time"}`. `email` se valida con el `pattern` del esquema.
- **REQ-SCHEMA-7.** Los esquemas DEBEN viajar dentro del paquete `aucove`; el núcleo NO DEBE descargarlos de la red (los `$id` de `aucove.invalid` son solo identificadores).
- **REQ-SCHEMA-8.** El validador DEBE informar todos los problemas de un archivo, no solo el primero, salvo un error de sintaxis, que detiene el análisis de ese archivo.
- **REQ-SCHEMA-9.** Cada violación del esquema DEBE informarse como `E_VALIDATION_SCHEMA`, con la ruta JSON Pointer (RFC 6901) del valor.

### 2.4 Registro de problema

Los validadores producen una lista de problemas con esta forma. Cómo se entrega lo define 05-operaciones: dentro de `validate`, cada problema se proyecta en un elemento de `data_validate.results[].findings` (`path` = `file`, `line`, `severity`, `message`, `code`, `pointer`, `hint`); `results[].check` es el ID de regla de 09-ci §4.1. Fuera de `validate`, una operación que encuentra `project.yaml` o `roles.yaml` inválido falla con `E_CONFIG_INVALID` (05-operaciones). Los códigos `W_` de este capítulo son códigos de problema con `severity: warning`, no avisos del sobre (`warnings[]`, catálogo cerrado de 05-operaciones).

```json
{
  "code": "E_VALIDATION_UNKNOWN_PERSON",
  "severity": "error",
  "file": "roles.yaml",
  "pointer": "/sections/2/owner",
  "line": 31,
  "message": "La sección 'resultados' tiene como dueño a 'carlasoto', que no está en people.",
  "hint": "Corrige el login o agrega la persona a people."
}
```

- **REQ-SCHEMA-10.** `severity` DEBE ser `error` o `warning`; los códigos de error empiezan con `E_` y los de advertencia con `W_` (§10). Un archivo con al menos un `error` es inválido.

### 2.5 Qué versión se lee

- **REQ-SCHEMA-11.** Toda decisión de autorización o de pertenencia (personas, dueños, secciones existentes) DEBE resolverse contra `roles.yaml` de la **rama base** (D-08). La versión del PR solo se usa para validar su formato y la coherencia del árbol del PR (tabla siguiente; 09-ci REQ-CI-25).

| Chequeo | Versión que se usa |
|---|---|
| Formato de un archivo que el PR modifica | la del PR (head) |
| Reglas internas de un archivo (p. ej. REQ-SCHEMA-24 a 30 en `roles.yaml`) | la misma versión del archivo |
| Personas, dueños y secciones que citan otros archivos | base |
| Coherencia del árbol con `roles.yaml` (`conv.section.*`, `conv.main.sections`, opciones de `task.yml`, `CODEOWNERS`) | `roles.yaml` de head si el PR lo modifica y es válido; si no, base |
| IDs del plan que citan un Issue, una decisión o el bloque del PR | head si el PR modifica `plan.md` y ese `plan.md` es válido; si no, base |
| Reglas entre versiones de `plan.md` (§5.4) | base frente a head |
| Registros que se pueden reemplazar (`supersedes`) | base |

La clasificación de rutas, el alcance y la autorización usan siempre la base (D-08).

### 2.6 Identificadores comunes

| Nombre | Patrón | Notas |
|---|---|---|
| slug | `^[a-z0-9]+(-[a-z0-9]+)*$` | secciones (≤ 40), `name` (≤ 64), decisiones (≤ 48) |
| login | `^[A-Za-z0-9]+(-[A-Za-z0-9]+)*(_[A-Za-z0-9]+)?$`, ≤ 39 | reglas de GitHub; `_shortcode` de Enterprise Managed Users |
| ID del plan | `^(RQ\|HYP\|IN\|OUT\|METH)-[1-9][0-9]{0,3}$` | §5 |
| ID de decisión | `^[0-9]{4}-[0-9]{2}-[0-9]{2}-[1-9][0-9]*-<slug>$`, ≤ 72 | §6 |
| número de tarea | entero ≥ 1 | número del Issue |

- **REQ-SCHEMA-12.** Los `login` DEBEN compararse sin distinguir mayúsculas (`str.lower()`, suficiente porque el patrón es ASCII); los artefactos generados (CODEOWNERS, menciones) DEBEN usar la grafía de `people[].login`.
- **REQ-SCHEMA-13.** Todo slug que el núcleo derive de texto libre DEBE producirse con esta función:

```python
import re, unicodedata

def slugify(text: str, max_len: int = 48) -> str:
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()   # ñ -> n, á -> a
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t[:max_len].rstrip("-") or "decision"
# slugify("Fusionar RQ-3 en RQ-1: ¿año base 2019?") == "fusionar-rq-3-en-rq-1-ano-base-2019"
```

## 3. `project.yaml`

### 3.1 Ejemplo completo

```yaml
# yaml-language-server: $schema=https://aucove.invalid/schemas/project/v1
convention: "0.1"
core: ">=0.1.3,<0.2"
core_pin: "0.1.3"
name: movilidad-teletrabajo
title: "Movilidad urbana y teletrabajo en ciudades intermedias"
language: es
format: latex
disclosure: milestones
forge:
  type: github
  repo: lab-movilidad/movilidad-teletrabajo
  base_branch: main
build:
  engine: pdflatex
  main: tex/main.tex
  image: registry.gitlab.com/islandoftex/images/texlive:TL2026-2026-09-20-full@sha256:7334b00bf8e7a0996f7ddd65482363aaf7711d372e569f3ea78509619e3083ff
  bibliography: biber
  bib_files:
    - bib/references.bib
  shell_escape: disabled
  compile_on_submit: if_available
  timeout_minutes: 10
limits:
  max_active_claims: 3
  claim_expiry_days: 14
  expiry_warning_days: 3
  handoff_hold_days: 7
  exclusive_sections: false
context:
  max_chars: 8000
  recent_decisions: 5
coordinator:
  require_direction_ack: false
review:
  sla_hours: 72
data_policy:
  level: internal
  require_commercial: true     # es el valor por defecto si disclosure no es open (D-48)
  human_subjects: true
  ethics_ref: "CEC-2026-114"
licenses:
  holder: "Los autores de movilidad-teletrabajo"
  text: LicenseRef-AllRightsReserved
  code: MIT
  data: LicenseRef-AllRightsReserved
ci:
  pdf_retention_days: 14
  latexdiff: false
  model_review:
    enabled: false
```

Proyecto sin manuscrito (solo lo que cambia):

```yaml
format: none            # sin bloque build; ci.pdf_retention_days y ci.latexdiff no tienen efecto (REQ-SCHEMA-90)
disclosure: milestones
```

Proyecto individual (solo lo que cambia; 00-marco D-57):

```yaml
mode: individual        # una sola persona activa en roles.yaml; los PR se integran sin aprobación (REQ-SCHEMA-94)
```

Firma del proyecto (solo el bloque que cambia; 00-marco D-72):

```yaml
evidence_signing:
  mode: ci                # ci: firma el CI; admin: firma el admin en cada hito; none: sin firma
  keys:
    - id: k1
      public_key: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA1L20vZkfUKSNHpRKBXE1uvnrNgGT54K/5dSLLFhxtl lab-movilidad"
      since: "2026-10-14T00:00:00Z"
      until: null         # la fecha desde la que la llave deja de valer, al rotarla o revocarla
```

Con perfil `advisory`, `init` agrega además la aceptación explícita del responsable:

```yaml
forge:
  type: github
  repo: lab-movilidad/movilidad-teletrabajo
  base_branch: main
  advisory_acceptance:
    by: ana-perez
    at: "2026-10-01T14:05:00Z"
```

Revisión con modelo activada (solo el bloque que cambia; clave reservada, sin efecto en la plantilla v0.1):

```yaml
ci:
  model_review:
    enabled: true
    auth: oidc
    model: claude-sonnet-5
    max_turns: 4
```

### 3.2 Claves, valores por defecto y validaciones

| Clave | Tipo | Req. | Defecto | Validación y uso |
|---|---|---|---|---|
| `convention` | cadena | sí | — | `"<mayor>.<menor>"`; v0.1 = `"0.1"`. Compatibilidad: 01-arquitectura |
| `core` | cadena | sí | — | especificador PEP 440 sin `===`, p. ej. `">=0.1.3,<0.2"`; lo escribe `init` (01-arquitectura REQ-ARCH-44) |
| `core_pin` | `"X.Y.Z"`, o una pre-release de PEP 440 (`"X.Y.ZrcN"`, `aN`, `bN`; 00-marco D-66) | sí | — | versión exacta que instalan CI, devcontainer y nube; satisface `core` (01-arquitectura §6.2) |
| `name` | slug ≤ 64 | sí | — | nombre corto; aparece en mensajes y métricas |
| `title` | cadena 1..200 | sí | — | título legible |
| `language` | `es` \| `en` | sí | — | idioma de skills, mensajes y plantillas (D-02) |
| `format` | `latex` \| `none` | sí | — | `latex`: manuscrito en `tex/`, compilado por el CI; `none`: sin manuscrito y con el texto de cada parte en `<id>.md` (D-43); con `none` no se admite `build` (REQ-SCHEMA-90) |
| `mode` | `group` \| `individual` | no | `group` | `individual`: proyecto de una sola persona; `roles.yaml` tiene una sola persona activa, el responsable, y los PR se integran sin aprobación, con el CI y los sellos de siempre (00-marco D-57; REQ-SCHEMA-94). Lo pone `init --individual`; para pasar a grupo se cambia a `group` |
| `disclosure` | `private` \| `milestones` \| `open` | no | `milestones` | política de apertura (D-44; checklist en 11-trazabilidad §8.9); `open` exige `data_policy.level: public` (REQ-SCHEMA-92) |
| `evidence_signing.mode` | `ci` \| `admin` \| `none` | sí, si está el bloque | `none` (sin el bloque) | quién firma con la llave del proyecto: `ci`, el job `integration` de `seal.yml`; `admin`, el admin en cada hito; `none`, nadie (00-marco D-72; 11-trazabilidad §8.10) |
| `evidence_signing.keys` | lista de llaves | si `mode` no es `none` | — | llaves públicas del proyecto, sin borrar ninguna (REQ-SCHEMA-96; 11-trazabilidad REQ-TRACE-93) |
| `evidence_signing.keys[].id` | cadena `^[a-z0-9][a-z0-9-]{0,31}$` | sí | — | identificador de la llave; no se repite |
| `evidence_signing.keys[].public_key` | `ssh-ed25519 <base64>`, con un comentario opcional | sí | — | la llave pública, como la escribe `ssh-keygen` |
| `evidence_signing.keys[].since` / `.until` | fecha-hora RFC 3339 en UTC / ídem o `null` | sí / no | — / `null` | la llave vale desde `since` y antes de `until` |
| `forge.type` | `github` | sí | — | único valor en v0.1 (D-04) |
| `forge.repo` | `owner/name` | sí | — | repo canónico; sin distinguir mayúsculas |
| `forge.base_branch` | cadena | no | `main` | nombre de rama válido para `git check-ref-format --branch` (no empieza con `-`, ningún componente termina en `.lock`, no es `HEAD`); no empieza con `task/`, sin distinguir mayúsculas; DEBE ser la rama por defecto del repositorio (REQ-SCHEMA-19) |
| `forge.advisory_acceptance.by` / `.at` | login / fecha-hora RFC 3339 | solo en `advisory` | — | aceptación del perfil `advisory` (06-permisos REQ-PERM-41) |
| `build.engine` | `pdflatex` \| `xelatex` \| `lualatex` | no | `pdflatex` | solo con `format: latex`; 09-ci lo traduce a opciones de latexmk |
| `build.main` | ruta `.tex` | no | `tex/main.tex` | solo con `format: latex`; relativa, sin `..`; debe existir. En la convención 0.1 solo vale `tex/main.tex` (02-convencion §4, regla `conv.convention.value`), que cumple el patrón de 09-ci §3.2 (`^tex/[A-Za-z0-9._-]+\.tex$`) |
| `build.image` | cadena `^[^\s@]+@sha256:[0-9a-f]{64}$` | no | la imagen de 09-ci REQ-CI-17 | solo con `format: latex`; imagen de compilación fijada por digest; 09-ci la lee de la base (REQ-CI-16) |
| `build.bibliography` | `biber` \| `bibtex` \| `none` | no | `biber` | solo con `format: latex` |
| `build.bib_files` | lista de rutas `.bib` | no | `[bib/references.bib]` | solo con `format: latex`; ≤ 20, sin repetidos; deben existir |
| `build.shell_escape` | `disabled` \| `restricted` | no | `disabled` | solo con `format: latex`; `-no-shell-escape` o `-shell-restricted` (10-seguridad REQ-SEC-54); no hay modo sin restricción |
| `build.compile_on_submit` | `never` \| `if_available` \| `always` | no | `if_available` | solo con `format: latex`; compilación local al entregar (D-14); efecto en 05-operaciones |
| `build.timeout_minutes` | entero 1..12 | no | `10` | solo con `format: latex`; tope de una compilación |
| `limits.max_active_claims` | entero 1..10 | no | `3` | semántica en 04-tareas |
| `limits.claim_expiry_days` | entero 1..90 | no | `14` | semántica en 04-tareas |
| `limits.expiry_warning_days` | entero 1..30 | no | `3` | menor que `claim_expiry_days`; semántica en 04-tareas |
| `limits.handoff_hold_days` | entero 1..30 | no | `7` | vigencia de un traspaso; semántica en 04-tareas |
| `limits.exclusive_sections` | booleano | no | `false` | una sola tarea activa por sección; semántica en 04-tareas |
| `context.max_chars` | entero 2000..9000 | no | `8000` | tope del paquete de `get_context`; margen para el envoltorio de SessionStart; el tope de Claude Code es 10 000 |
| `context.recent_decisions` | entero 0..20 | no | `5` | decisiones vigentes que entran al contexto |
| `coordinator.require_direction_ack` | booleano | no | `false` | activa el check `direction-ack` (04-tareas, 09-ci) |
| `review.sla_hours` | entero 1..720 | no | `72` | SLA de revisión (06-permisos) |
| `data_policy.level` | `public` \| `internal` | no | `internal` | nivel de dato; matriz con `account_type` en 10-seguridad §8 |
| `data_policy.require_commercial` | booleano | no | `true` si `disclosure` no es `open`; si no, `false` (REQ-SCHEMA-91) | 10-seguridad §8.3 (C1); D-48 |
| `data_policy.human_subjects` | booleano | no | `false` | si es `true`, `ethics_ref` es obligatorio |
| `data_policy.ethics_ref` | cadena \| `null` | si `human_subjects` | `null` | identificador del dictamen de ética |
| `licenses.holder`, `.text`, `.code`, `.data` | cadena; SPDX o `LicenseRef-*` | no (las cuatro juntas) | — | licencias del proyecto (11-trazabilidad §7.3) |
| `ci.pdf_retention_days` | entero 1..90 | no | `14` | retención de los PDF de PR; los de la base usan 90 (09-ci REQ-CI-19); sin efecto con `format: none` |
| `ci.latexdiff` | booleano | no | `false` | PDF de diferencias contra la base (09-ci REQ-CI-23); sin efecto con `format: none` |
| `ci.model_review.enabled` | booleano | no | `false` | reservada: sin efecto en la plantilla v0.1 (09-ci §8); D-15 |
| `ci.model_review.auth` | `api_key` \| `oidc` | si `enabled` | — | nunca tokens de suscripción (D-15) |
| `ci.model_review.model` | cadena `^claude-[a-z0-9-]+$` | si `enabled` | — (sin valor por defecto) | ID del modelo |
| `ci.model_review.max_turns` | entero 1..4 | no | `4` | |

Claves y valores reservados (el esquema v1 los rechaza): `forge.host` y otros valores de `forge.type` (GitLab, Forgejo, GitHub Enterprise Server); `format: markdown | quarto`; `data_policy.level: restricted | local_only` (10-seguridad REQ-SEC-63); `ci.model_review.provider`, `ci.model_review.environment` y `ci.model_review.trigger`; la clave `evidence` (el sellado no se configura en v0.1; 00-marco D-45, D-47). La firma del proyecto sí se configura, con `evidence_signing` (D-72).

### 3.3 Reglas

- **REQ-SCHEMA-14.** `project.yaml` DEBE validar contra `project.schema.json`; toda clave desconocida es `E_VALIDATION_SCHEMA` (`additionalProperties: false`).
- **REQ-SCHEMA-15.** El núcleo DEBE construir la configuración efectiva aplicando los valores por defecto de la tabla §3.2; los `default` del esquema son informativos y ningún validador los inyecta.
- **REQ-SCHEMA-16.** Si `convention`, `core` o `core_pin` se escriben sin comillas (`convention: 0.1` se lee como número y `core: >=0.1` es un error de sintaxis YAML), el problema DEBE incluir el `hint` "escríbelo entre comillas".
- **REQ-SCHEMA-17.** El núcleo DEBE fallar con `E_CONFIG_CORE_MISMATCH` si su versión no satisface `core` (evaluado con `packaging.specifiers.SpecifierSet`), y con `E_CONFIG_CONVENTION_UNSUPPORTED` (05-operaciones) si no admite `convention` según la regla de 01-arquitectura.
- **REQ-SCHEMA-83.** `core_pin` DEBE satisfacer `core` y aparecer en una sola línea de nivel superior que cumpla `^core_pin: "[0-9]+\.[0-9]+\.[0-9]+((a|b|rc)[0-9]+)?"( *#.*)?$` (01-arquitectura REQ-ARCH-45); si no, `E_CONFIG_INVALID`.
- **REQ-SCHEMA-84.** `limits.expiry_warning_days` DEBE ser menor que `limits.claim_expiry_days` en la configuración efectiva (`E_CONFIG_INVALID`).
- **REQ-SCHEMA-18.** `forge.repo` DEBE coincidir sin distinguir mayúsculas con el remoto `origin` en local y con el repositorio que ejecuta el workflow en CI (`E_CONFIG_REMOTE_MISMATCH`, 05-operaciones).
- **REQ-SCHEMA-19.** `forge.base_branch` DEBE ser la rama por defecto del repositorio; `validate` e `init` DEBEN fallar con `E_CONFIG_BASE_NOT_DEFAULT`. Motivo: GitHub solo enlaza y cierra Issues con `Closes #<n>` en PR que apuntan a la rama por defecto.
- **REQ-SCHEMA-20.** Con `format: latex`, `build.main` y cada `build.bib_files[]` DEBEN existir en el árbol validado (`E_CONFIG_PATH_NOT_FOUND`).
- **REQ-SCHEMA-21.** `project.yaml` NO DEBE contener credenciales: `ci.model_review` no nombra credenciales, y ningún valor de `auth` corresponde a tokens de suscripción (D-15).
- **REQ-SCHEMA-85.** Si `data_policy.human_subjects` es `true`, `data_policy.ethics_ref` DEBE ser una cadena no vacía (lo hace cumplir el esquema; 10-seguridad REQ-SEC-67). Qué `account_type` admite cada `data_policy.level` y el código `E_VALIDATION_ACCOUNT_TYPE` los define 10-seguridad.
- **REQ-SCHEMA-90.** Con `format: none`, `project.yaml` NO DEBE tener el bloque `build` (lo exige el esquema), y `ci.pdf_retention_days` y `ci.latexdiff` no tienen efecto.
- **REQ-SCHEMA-91.** En la configuración efectiva, si falta `data_policy.require_commercial`, vale `true` cuando `disclosure` no es `open` y `false` cuando lo es. Un `false` explícito con `disclosure` distinto de `open` DEBERÍA ir acompañado de una decisión en `memory/decisions/` (D-48). La regla `schemas` aplica la matriz de 10-seguridad §8.3 con este valor efectivo.
- **REQ-SCHEMA-92.** Con `disclosure: open`, `data_policy.level` DEBE ser `public` (lo hace cumplir el esquema; 10-seguridad REQ-SEC-85).
- **REQ-SCHEMA-93.** Los esquemas `evidence-manifest/v1`, `evidence-openings/v1`, `evidence-record/v1` y `evidence-signature/v1` se empaquetan con el núcleo. Su semántica la define 11-trazabilidad §8.
- **REQ-SCHEMA-96.** Con `evidence_signing.mode` distinto de `none`, `keys` DEBE tener al menos una llave (lo hace cumplir el esquema). El núcleo DEBE rechazar con `E_CONFIG_INVALID` un `id` o una `public_key` repetidos, una `public_key` que no sea una llave `ssh-ed25519` válida y un `until` que no sea posterior a su `since`. La lista que publica el grupo y que lee `evidence verify --keys` es un YAML con solo la clave `keys`, con las mismas reglas (11-trazabilidad §8.10).

## 4. `roles.yaml`

### 4.1 Ejemplo completo

```yaml
# yaml-language-server: $schema=https://aucove.invalid/schemas/roles/v1
version: 1
lead: ana-perez
deputy: bruno-diaz          # coincide con "Bruno-Diaz": los login no distinguen mayúsculas
editor: carla-soto          # editor del artículo (D-56); si falta, lo es el responsable
people:
  - login: ana-perez
    name: "Ana Pérez Rojas"
    email: ana.perez@uni.example
    orcid: "0000-0002-1825-0097"
    role: lead
    account_type: team
    credit: [conceptualization, funding-acquisition, supervision, writing-review-editing]
  - login: Bruno-Diaz
    name: "Bruno Díaz"
    role: researcher
    account_type: enterprise
    credit: [methodology, formal-analysis, writing-original-draft]
  - login: carla-soto
    name: "Carla Soto"
    email: csoto@uni.example
    role: researcher
    account_type: consumer_training_off
    credit: [investigation, data-curation]
  - login: diego-luna
    name: "Diego Luna"
    role: researcher
    account_type: api
    active: false             # dejó el proyecto; conserva su crédito
    credit: [software]
sections:
  - id: introduccion
    title: "Introducción"
    owner: ana-perez
  - id: metodos
    title: "Métodos"
    owner: Bruno-Diaz
  - id: resultados
    title: "Resultados"
    owner: carla-soto
```

Este ejemplo es válido y produce una sola advertencia: `W_VALIDATION_LOGIN_CASE` en `/deputy`.

### 4.2 Claves

| Clave | Tipo | Req. | Notas |
|---|---|---|---|
| `version` | `1` | sí | versión del formato |
| `lead` | login | sí | responsable (00-marco §4) |
| `deputy` | login \| `null` | no (defecto `null`) | suplente (D-17) |
| `editor` | login \| `null` | no (defecto `null`: lo es el `lead`) | editor del artículo, solo con `format: latex` (00-marco D-56; REQ-SCHEMA-95) |
| `people[].login` | login | sí | identidad: usuario de la forja |
| `people[].forge_id` | entero ≥ 1 | no | id durable de la cuenta; si existe, la identidad se compara primero por id (08-forja) |
| `people[].name` | cadena 1..200 | sí | nombre para autoría y CRediT |
| `people[].email` | email ≤ 254 | no | se versiona: DEBE ser la dirección noreply de la forja desde el día 1, porque la historia sellada no se reescribe antes de abrir (D-44); `doctor` lo revisa (check `identity`) |
| `people[].orcid` | `0000-0000-0000-000X` | no | sin prefijo `https://orcid.org/` |
| `people[].role` | `lead` \| `researcher` | sí | rol de permiso (00-marco §5.6) |
| `people[].account_type` | §4.4 | sí | régimen de datos de su cuenta de IA |
| `people[].credit` | lista de §4.3 | no (defecto `[]`) | roles CRediT declarados |
| `people[].active` | booleano | no (defecto `true`) | `false` = baja (ver 10-seguridad) |
| `sections[].id` | slug ≤ 40, distinto de `none` | sí | contenido en `sections/<id>/`; el texto visible la llama «parte» (D-43) |
| `sections[].title` | cadena 1..120 | sí | |
| `sections[].owner` | login | sí | único dueño |

`none` se prohíbe como `id` porque GitHub reserva "None" en las opciones de los formularios para indicar que no se eligió nada (§8).

La plantilla trae las 7 partes de 00-marco D-55, con el responsable de ejemplo como dueño: `introduction`, `background`, `theory`, `methodology`, `experiments`, `results` y `conclusions`. Si una sección del artículo necesita varios autores, se divide en subpartes con su propio `id` y su dueño (por ejemplo, `experiments-a` y `experiments-b`); con `format: latex`, el editor las agrupa en `tex/outline/<id>.tex` (02-convencion).

### 4.3 Roles CRediT

Los 14 identificadores son el último segmento de la URI de cada rol en `https://credit.niso.org/contributor-roles/<id>/` (ANSI/NISO Z39.104-2022). La traducción es informativa.

| `credit` | Nombre oficial | Español |
|---|---|---|
| `conceptualization` | Conceptualization | Conceptualización |
| `data-curation` | Data curation | Curación de datos |
| `formal-analysis` | Formal analysis | Análisis formal |
| `funding-acquisition` | Funding acquisition | Obtención de financiamiento |
| `investigation` | Investigation | Investigación |
| `methodology` | Methodology | Metodología |
| `project-administration` | Project administration | Administración del proyecto |
| `resources` | Resources | Recursos |
| `software` | Software | Software |
| `supervision` | Supervision | Supervisión |
| `validation` | Validation | Validación |
| `visualization` | Visualization | Visualización |
| `writing-original-draft` | Writing – original draft | Redacción: borrador original |
| `writing-review-editing` | Writing – review & editing | Redacción: revisión y edición |

El grado de contribución (lead, equal, supporting) queda reservado para `roles/v2` (Q-SCHEMA-2); no es parte de ANSI/NISO Z39.104-2022 (https://credit.niso.org/implementing-credit/).

### 4.4 Valores de `account_type`

Clasifican el **régimen de datos** de la cuenta con la que la persona usa IA en este proyecto, no el proveedor. Son los valores de 10-seguridad §8.2 más `none`; la matriz que dice qué valor admite cada `data_policy.level` es de 10-seguridad §8.3.

| Valor | Significado | Ejemplos |
|---|---|---|
| `consumer_training_on` | Plan de consumo con uso para entrenamiento activado | Claude Free, Pro o Max (también en Claude Code) con el ajuste activo: retención de 5 años |
| `consumer_training_off` | Plan de consumo con el entrenamiento desactivado | ídem con el ajuste desactivado: retención de 30 días |
| `team` | Plan comercial de organización | Claude Team |
| `enterprise` | Plan comercial empresarial o de gobierno | Claude Enterprise, Claude Gov |
| `api` | API key propia o institucional bajo términos comerciales | Claude API |
| `cloud_provider` | Modelo por una nube bajo contrato institucional | Amazon Bedrock, Google Cloud, Microsoft Foundry |
| `local` | Modelo en infraestructura propia; los datos no salen | modelo local vía goose u otra puerta |
| `none` | No usa IA en el proyecto | |
| `unknown` | No declarado todavía | |

- **REQ-SCHEMA-22.** Si una persona usa más de un tipo de cuenta en el proyecto, `account_type` DEBE declarar el de menor protección según el orden de 10-seguridad §8.2: `consumer_training_on` < `unknown` < `consumer_training_off` < `api` = `team` < `enterprise` = `cloud_provider` < `local` < `none`.

### 4.5 Reglas

- **REQ-SCHEMA-23.** `roles.yaml` DEBE validar contra `roles.schema.json`.
- **REQ-SCHEMA-24.** Exactamente una persona DEBE tener `role: lead`, estar activa y coincidir con `lead`; si no, `E_VALIDATION_ROLES_LEAD`.
- **REQ-SCHEMA-25.** Si `deputy` no es `null`, DEBE nombrar a una persona activa de `people`, con `role: researcher` y distinta del `lead`; si no, `E_VALIDATION_ROLES_DEPUTY`.
- **REQ-SCHEMA-26.** Los `login` de `people` DEBEN ser únicos sin distinguir mayúsculas (`E_VALIDATION_DUPLICATE_LOGIN`).
- **REQ-SCHEMA-27.** Los `sections[].id` DEBEN ser únicos (`E_VALIDATION_DUPLICATE_ID`).
- **REQ-SCHEMA-28.** Cada `sections[].owner` DEBE existir en `people` (`E_VALIDATION_UNKNOWN_PERSON`) y estar activo (`E_VALIDATION_INACTIVE_PERSON`).
- **REQ-SCHEMA-29.** Un `orcid` DEBE tener un dígito de control válido según ISO/IEC 7064 MOD 11-2 (`E_VALIDATION_ORCID`).
- **REQ-SCHEMA-30.** Una referencia (`lead`, `deputy`, `editor`, `owner`) cuya grafía difiere de `people[].login` solo en mayúsculas DEBE producir `W_VALIDATION_LOGIN_CASE`.
- **REQ-SCHEMA-31.** Cada parte declarada DEBE tener su directorio `sections/<id>/`, y todo directorio de `sections/` DEBE corresponder a una parte declarada. Lo verifican `conv.section.missing` y `conv.section.undeclared` de 02 §12 (severidad `error`).
- **REQ-SCHEMA-32.** El núcleo NO DEBE usar `email` como identidad ni exigirlo; la identidad es `login` (la obtiene 08-forja).
- **REQ-SCHEMA-33.** CRediT DEBE tomarse solo de `people[].credit`; el núcleo NO DEBE inferir roles CRediT del historial de git (11-trazabilidad).
- **REQ-SCHEMA-94.** Con `mode: individual` (§3.2; 00-marco D-57), `roles.yaml` DEBE tener una sola persona activa, la del `lead`, y `deputy: null`; si no, `E_VALIDATION_ROLES_MODE` en `/people` o en `/deputy`. Las personas inactivas pueden quedar, porque conservan su crédito. Motivo: el modo quita la aprobación de los PR, así que no debe quedar activo por accidente cuando hay más personas. La regla lee `project.yaml` y `roles.yaml` de la misma versión (§2.5).
- **REQ-SCHEMA-95.** Si `editor` no es `null`, DEBE nombrar a una persona activa de `people`, de cualquier rol; si no, `E_VALIDATION_UNKNOWN_PERSON` o `E_VALIDATION_INACTIVE_PERSON` en `/editor`. Si es `null`, el editor es el `lead` (00-marco D-56). Con `format: none`, `editor` no tiene efecto y `validate` da el aviso `conv.format.unused` (02-convencion REQ-CONV-88).

```python
def orcid_ok(orcid: str) -> bool:                     # ISO/IEC 7064 MOD 11-2
    d = orcid.replace("-", "")
    total = 0
    for ch in d[:-1]:
        total = (total + int(ch)) * 2
    check = (12 - total % 11) % 11
    return d[-1] == ("X" if check == 10 else str(check))
```

Las reglas REQ-SCHEMA-24 a 30, 94 y 95 corren después del esquema, sobre el documento ya validado, e informan todos los problemas con su JSON Pointer (por ejemplo `/sections/2/owner`).

`roles.yaml` es la única fuente de personas y dueños (D-08). `CODEOWNERS`, las opciones del formulario de tarea (§8.1) y las reglas locales se generan a partir de él (06-permisos, 07-hooks, 09-ci).

## 5. Sintaxis de `plan.md`

### 5.1 Ejemplo

````markdown
# Plan del proyecto

<!-- Solo lo edita el responsable. Sintaxis: 03-esquemas §5.
- RQ-99: esto no es una definición: está dentro de un comentario.
-->

## Objetivo

Estimar cómo cambió la movilidad diaria tras la adopción del teletrabajo en
ciudades intermedias entre 2019 y 2025.

## Preguntas de investigación

- RQ-1: ¿Cuánto se redujeron los viajes pendulares entre 2019 y 2025?
- RQ-2: ¿Qué grupos de ingreso concentran la reducción?
  Se responde con las dos olas de la encuesta origen-destino.
- RQ-3 (retired: 2026-10-06-12-fusionar-rq3-en-rq1): ~~¿Cambió el número de viajes por motivo trabajo?~~

## Hipótesis

- HYP-1: La reducción es mayor en los quintiles de ingreso altos (RQ-2).
- HYP-2 (retired): ~~El efecto desaparece en 2024.~~

## Alcance

### Dentro

- IN-1: Ciudades de 100 000 a 500 000 habitantes.
- IN-2: Viajes de personas de 18 a 65 años.

### Fuera

- OUT-1: Transporte de carga y logística.

## Métodos

- METH-1: Diferencias en diferencias entre ciudades con y sin alta adopción del teletrabajo.
- METH-2: Ponderación por diseño muestral de la encuesta (IN-2).
````

Los encabezados son libres y se localizan; el analizador no depende de ellos. El tipo de un elemento lo da su prefijo (00-marco §5.9).

### 5.2 Gramática

```text
definición  = "- " id [ " (retired" [ ": " id-decisión ] ")" ] ": " texto
id          = ( "RQ" | "HYP" | "IN" | "OUT" | "METH" ) "-" número   ; número 1..9999 sin ceros a la izquierda
texto       = carácter no blanco, luego cualquier texto hasta el fin de línea
detalle     = líneas siguientes con sangría de 2 o más espacios, hasta una línea en blanco o sin sangría
```

- **REQ-SCHEMA-34.** Una definición DEBE ser una línea sin sangría que cumpla `^- (?P<id>(?:RQ|HYP|IN|OUT|METH)-[1-9][0-9]{0,3})(?P<ret> \(retired(?:: (?P<dec><id-decisión>))?\))?: (?P<text>\S.*)$`.
- **REQ-SCHEMA-35.** El analizador DEBE ignorar las líneas dentro de bloques de código delimitados (` ``` ` o `~~~`) y de comentarios HTML (`<!-- ... -->`).
- **REQ-SCHEMA-36.** Una línea que parece una definición pero no cumple la gramática DEBE producir `E_VALIDATION_PLAN_SYNTAX`; "parece" significa que cumple `^\s*(?:[-*+]\s*[*_~]*\s*(?i:RQ|HYP|IN|OUT|METH)\s*-\s*[0-9]+|[#*_~]*\s*(?:RQ|HYP|IN|OUT|METH)-[0-9]+\s*[:.)])`. Casos: `- rq-2: …`, `* HYP-1: …`, `- RQ-04: …`, `- HYP-2 texto` (sin dos puntos), `RQ-5: …` (sin viñeta), `### HYP-3: …`, y una definición con sangría (`  - RQ-9: …`).
- **REQ-SCHEMA-37.** Un ID definido dos veces DEBE producir `E_VALIDATION_DUPLICATE_ID`.
- **REQ-SCHEMA-38.** Los IDs citados dentro del texto o del detalle (`\b(RQ|HYP|IN|OUT|METH)-[1-9][0-9]{0,3}\b`) DEBEN existir en el mismo `plan.md` (`E_VALIDATION_UNKNOWN_PLAN_ID`).

### 5.3 Elementos retirados

Un elemento retirado **no se borra**: se marca con `(retired)` después del ID, opcionalmente con el ID de la decisión que lo retiró. El texto conserva su redacción original y PUEDE ir tachado con `~~…~~`, que el analizador quita.

| Estado | Línea |
|---|---|
| Activo | `- RQ-3: ¿Cambió el número de viajes por motivo trabajo?` |
| Retirado | `- RQ-3 (retired): ~~¿Cambió el número de viajes por motivo trabajo?~~` |
| Retirado, con decisión | `- RQ-3 (retired: 2026-10-06-12-fusionar-rq3-en-rq1): ~~¿Cambió…?~~` |

- **REQ-SCHEMA-39.** El ID de decisión de un retiro DEBE existir en `memory/decisions/` de la base o del mismo PR (`E_VALIDATION_UNKNOWN_DECISION`).

### 5.4 Reglas entre versiones (base → head)

Cuando un PR modifica `plan.md`:

- **REQ-SCHEMA-40.** Todo ID presente en la base DEBE seguir presente en el PR (`E_VALIDATION_PLAN_ID_REMOVED`).
- **REQ-SCHEMA-41.** Un ID retirado en la base DEBE seguir retirado, con el mismo texto; solo PUEDE agregarse la decisión si no tenía (`E_VALIDATION_PLAN_RETIRED_CHANGED`).
- **REQ-SCHEMA-42.** Un ID nuevo DEBE tener un número mayor que el máximo de su prefijo en la base, contando los retirados (`E_VALIDATION_PLAN_ID_REUSED`).
- **REQ-SCHEMA-43.** Un cambio en el texto o el detalle de un elemento activo DEBE producir `W_VALIDATION_PLAN_TEXT_CHANGED` con el ID; 04-tareas usa esa señal en el flujo de cambio de dirección.

### 5.5 Referencias desde otros archivos

- **REQ-SCHEMA-44.** Un ID del plan citado en un Issue de tarea, en una decisión o en el bloque del PR DEBE existir (`E_VALIDATION_UNKNOWN_PLAN_ID`); si está retirado, `W_VALIDATION_PLAN_REF_RETIRED`.

### 5.6 Analizador de referencia

```python
PLAN_ID = r"(?:RQ|HYP|IN|OUT|METH)-[1-9][0-9]{0,3}"
DEC_ID  = r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[1-9][0-9]*-[a-z0-9]+(?:-[a-z0-9]+)*"
DEF   = re.compile(rf"^- (?P<id>{PLAN_ID})(?P<ret> \(retired(?:: (?P<dec>{DEC_ID}))?\))?: (?P<text>\S.*)$")
LOOKS = re.compile(r"^\s*(?:[-*+]\s*[*_~]*\s*(?i:RQ|HYP|IN|OUT|METH)\s*-\s*[0-9]+"
                   r"|[#*_~]*\s*(?:RQ|HYP|IN|OUT|METH)-[0-9]+\s*[:.)])")
REF   = re.compile(rf"\b{PLAN_ID}\b")

def parse_plan(text):
    items, errors, fence, in_comment, cur = {}, [], None, False, None
    for no, line in enumerate(text.split("\n"), 1):
        s = line.strip()
        if in_comment:
            in_comment = "-->" not in line; continue
        if fence:
            fence = None if s.startswith(fence) else fence; continue
        if s.startswith(("```", "~~~")):
            fence, cur = s[:3], None; continue
        if s.startswith("<!--"):
            in_comment = "-->" not in s[4:]; continue
        if m := DEF.match(line):
            i = m["id"]
            if i in items:
                errors.append(("E_VALIDATION_DUPLICATE_ID", no, i)); cur = None; continue
            t, retired = m["text"].strip(), m["ret"] is not None
            if retired and len(t) > 4 and t.startswith("~~") and t.endswith("~~"):
                t = t[2:-2].strip()
            cur = items[i] = {"id": i, "kind": i.split("-")[0], "line": no, "detail": "",
                              "status": "retired" if retired else "active",
                              "retired_by": m["dec"], "text": t}
            continue
        if LOOKS.match(line):
            errors.append(("E_VALIDATION_PLAN_SYNTAX", no, s)); cur = None; continue
        if cur is not None and line.startswith("  ") and s:
            cur["detail"] += ("\n" if cur["detail"] else "") + s; continue
        cur = None
    for it in items.values():
        it["refs"] = sorted(set(REF.findall(it["text"] + "\n" + it["detail"])) - {it["id"]})
    return list(items.values()), errors
```

Salida de un elemento: `{"id": "RQ-3", "kind": "RQ", "status": "retired", "retired_by": "2026-10-06-12-fusionar-rq3-en-rq1", "text": "¿Cambió el número de viajes por motivo trabajo?", "detail": "", "refs": [], "line": 17}`.

## 6. Registro de decisión

### 6.1 Nombre de archivo

`memory/decisions/<YYYY-MM-DD>-<n>-<slug>.md`, por ejemplo `memory/decisions/2026-10-06-12-fusionar-rq3-en-rq1.md`.

| Alternativa | Problema |
|---|---|
| Secuencial (`D-0012-slug.md`) | Dos PR concurrentes calculan el mismo "siguiente número" y chocan. |
| Fecha + slug | Dos personas pueden elegir el mismo slug el mismo día (`cambiar-metodo`). |
| UUID o hash | No choca, pero es ilegible y no se ordena por fecha. |
| **Fecha + número de tarea + slug (elegida)** | `<n>` identifica una tarea, y cada tarea tiene una sola rama y un solo PR (D-10, D-12). Dos PR concurrentes nunca comparten `<n>`; dentro de un PR, el núcleo evita repetir el slug. El nombre se ordena por fecha y remite a la tarea. |

Si aun así dos PR agregan la misma ruta, git produce un conflicto de tipo add/add y la forja no deja integrar el segundo PR hasta resolverlo. Además, el check de inmutabilidad de 09-ci rechaza cualquier PR que modifique un registro ya integrado.

- **REQ-SCHEMA-45.** El nombre DEBE cumplir `^memory/decisions/(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})-(?P<n>[1-9][0-9]*)-(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)\.md$` con un slug de hasta 48 caracteres (`E_VALIDATION_DECISION_NAME`).
- **REQ-SCHEMA-46.** `<n>` DEBE ser el número de la rama `task/<n>` desde la que se agrega el registro; el front matter DEBE cumplir `id` = nombre sin `.md`, `date` = `<date>` y `task` = `<n>` (`E_VALIDATION_DECISION_NAME`).
- **REQ-SCHEMA-47.** `decision propose` DEBE formar el slug con `slugify(title)` (§2.6). Cuando el núcleo necesite un nombre alternativo para un registro propio (el `auto_rename` de 04-tareas), DEBE agregar `-2`, `-3`, etc. al slug hasta encontrar una ruta libre. Qué hace `decision propose` si la ruta ya existe lo define 05-operaciones (`E_VALIDATION_DECISION_EXISTS`).
- **REQ-SCHEMA-48.** En `memory/decisions/` solo PUEDEN existir registros con el nombre de REQ-SCHEMA-45 y un `.gitkeep` vacío; cualquier otro archivo es `E_VALIDATION_DECISION_NAME`. `memory/notes/` admite solo `task-<n>.md` y `.gitkeep`.
- **REQ-SCHEMA-49.** `date` DEBE ser la fecha local de la máquina que crea el registro y NO DEBE compararse con la fecha del merge.

### 6.2 Front matter

| Clave | Tipo | Req. | Regla |
|---|---|---|---|
| `id` | ID de decisión | sí | igual al nombre del archivo sin `.md` |
| `title` | cadena 1..120 | sí | origen del slug |
| `date` | `YYYY-MM-DD` | sí | igual a la fecha del nombre |
| `author` | login | sí | persona activa de `roles.yaml` (base) |
| `origin` | `agent` \| `human` | sí | `human` solo si lo creó la CLI con TTY y sin `CLAUDE_CODE_CHILD_SESSION` definida, o una persona a mano; en cualquier otro caso, `agent` (10-seguridad REQ-SEC-14) |
| `task` | entero ≥ 1 | sí | igual a `<n>` (REQ-SCHEMA-46) |
| `status` | `accepted` \| `rejected` | sí | sentido del registro al integrarse |
| `summary` | cadena 1..280 | sí | lo usa el paquete de contexto (07-hooks) |
| `supersedes` | lista de IDs de decisión | no (defecto `[]`) | vacía si `status: rejected` |
| `plan_refs` | lista de IDs del plan | no (defecto `[]`) | §5.5 |

`status: rejected` registra una opción que el equipo descartó de forma explícita, para que no se vuelva a discutir. Para revocar una decisión sin reemplazarla se escribe un registro `accepted` ("se deja sin efecto X") con `supersedes: [X]`.

### 6.3 Cuerpo

Encabezados de nivel 2, en el idioma del proyecto. El validador acepta ambos idiomas:

| Sección | `es` | `en` | Obligatoria |
|---|---|---|---|
| Contexto | `## Contexto` | `## Context` | sí |
| Decisión | `## Decisión` | `## Decision` | sí |
| Alternativas | `## Alternativas consideradas` | `## Alternatives considered` | no |
| Consecuencias | `## Consecuencias` | `## Consequences` | sí |

`decision propose` escribe las secciones en el orden de la tabla y omite Alternativas si no hay (05 §9.20).

### 6.4 Ejemplo completo

```markdown
---
id: 2026-10-06-12-fusionar-rq3-en-rq1
title: "Fusionar RQ-3 en RQ-1"
date: 2026-10-06
author: ana-perez
origin: human
task: 12
status: accepted
summary: "RQ-3 se retira porque los viajes por motivo trabajo ya se miden en RQ-1; el análisis se concentra en viajes pendulares."
supersedes: []
plan_refs: [RQ-1, RQ-3]
---

## Contexto

La encuesta de 2025 no distingue el motivo "trabajo" del motivo "estudio" en
viajes de más de 30 km, así que RQ-3 no se puede responder por separado.

## Decisión

Se retira RQ-3 de `plan.md` y su medición se incorpora a RQ-1.

## Alternativas consideradas

- Mantener RQ-3 con una imputación del motivo: descartada por el sesgo que introduce.

## Consecuencias

- La sección `resultados` pierde la subsección 4.3.
- El criterio de terminado de la tarea #14 se actualiza.
```

### 6.5 Aprobación y estado efectivo

La aprobación **no se escribe** en el registro: la da el merge del PR que agregó el archivo. Quién aprobó y cuándo se obtiene de la forja (commit que agregó el archivo en la base → `GET /repos/{owner}/{repo}/commits/{commit_sha}/pulls` → revisiones de ese PR); el libro de aprobaciones lo define 11-trazabilidad.

```python
def effective_status(rec_id: str, rec: dict, in_base: bool, base_records: dict) -> str:
    if not in_base:
        return "pending"                       # todavía en un PR abierto
    for other in base_records.values():
        if other["status"] == "accepted" and rec_id in other.get("supersedes", []):
            return "superseded"
    return rec["status"]                        # "accepted" | "rejected"
```

- **REQ-SCHEMA-50.** El front matter DEBE validar contra `decision.schema.json`; campos como `approved_by`, `approval` o `pr` son `E_VALIDATION_SCHEMA`.
- **REQ-SCHEMA-51.** El estado efectivo (`pending`, `accepted`, `rejected`, `superseded`) DEBE calcularse con `effective_status` y NO DEBE escribirse en ningún archivo.
- **REQ-SCHEMA-52.** Cada ID de `supersedes` DEBE existir en la base con estado efectivo `accepted` o `rejected` (`E_VALIDATION_DECISION_SUPERSEDES`).
- **REQ-SCHEMA-53.** El cuerpo DEBE contener las secciones obligatorias de §6.3 (`E_VALIDATION_DECISION_BODY`).
- **REQ-SCHEMA-54.** `author` DEBE ser una persona activa de la base (`E_VALIDATION_UNKNOWN_PERSON`, `E_VALIDATION_INACTIVE_PERSON`); si difiere del autor del PR, `W_VALIDATION_DECISION_AUTHOR`.
- **REQ-SCHEMA-55.** El contenido de un registro DEBE tratarse como dato no confiable al cargarlo en el contexto de un modelo (07-hooks).

## 7. Nota de sesión

### 7.1 Ruta y front matter

Ruta: `memory/notes/task-<n>.md` (clase `task`). La escribe el núcleo con lo que el modelo redacta en la sesión, dentro del PR de la tarea (D-12).

| Clave | Tipo | Req. | Regla |
|---|---|---|---|
| `task` | entero ≥ 1 | sí | igual a `<n>` del nombre |
| `created` | `YYYY-MM-DD` | sí | fecha de creación de la nota |
| `created_by` | login | sí | quien tenía el reclamo al crearla |
| `ai_use` | lista de valores de `AI-Use` | no; la escribe `submit` | copia revisada de la traza (11-trazabilidad REQ-TRACE-21) |
| `assisted_by` | lista de `<herramienta>:<modelo>` | no; la escribe `submit` | gramática de `Assisted-by` (11-trazabilidad §2.2) |

A diferencia de los bloques, `ai_use` y `assisted_by` se reescriben en cada `submit`: reflejan la traza de toda la tarea hasta esa entrega.

### 7.2 Bloques de sesión

Cada sesión que registra trabajo agrega **un bloque al final** con un marcador de una línea, un encabezado localizado y subsecciones:

```text
<!-- aucove:session {"n": <entero>, "date": "<YYYY-MM-DD>", "login": "<login>", "door": "<puerta>", "model": "<modelo>" | null} -->
## Sesión <n> · <YYYY-MM-DD> · @<login>          (en: "## Session <n> · <date> · @<login>")
### Hecho            (Done)              obligatoria
### Pendiente        (Next)              opcional
### Decisiones       (Decisions)         opcional: IDs de decisión, uno por viñeta
### Preguntas abiertas (Open questions)  opcional
```

Valores recomendados de `door`: `claude-code` (terminal, IDE, Desktop y web), `codex`, `antigravity-cli`, `gemini-cli`, `opencode`, `goose`, `forge-web` y `manual`.

### 7.3 Ejemplo completo

```markdown
---
task: 42
created: 2026-10-01
created_by: Bruno-Diaz
ai_use: [drafting, editing]
assisted_by: ["claude-code:claude-opus-5-5"]
---

<!-- aucove:session {"n": 1, "date": "2026-10-01", "login": "Bruno-Diaz", "door": "claude-code", "model": "claude-opus-5-5"} -->
## Sesión 1 · 2026-10-01 · @Bruno-Diaz

### Hecho

- Redacté la subsección 3.1 (fuentes de datos) en `sections/metodos/datos.tex`.

### Pendiente

- Describir la limpieza de la ola 2025.

### Decisiones

- 2026-10-01-42-usar-panel-balanceado

### Preguntas abiertas

- ¿Se reportan los factores de expansión en un apéndice?

<!-- aucove:session {"n": 2, "date": "2026-10-03", "login": "Bruno-Diaz", "door": "codex", "model": null} -->
## Sesión 2 · 2026-10-03 · @Bruno-Diaz

### Hecho

- Terminé la subsección 3.2 y respondí la revisión de @ana-perez.

### Pendiente

- Nada; queda lista para revisión.
```

### 7.4 Reglas

- **REQ-SCHEMA-56.** El front matter DEBE validar contra `session-note.schema.json` y `task` DEBE ser igual a `<n>` (`E_VALIDATION_NOTE`).
- **REQ-SCHEMA-57.** Cada bloque DEBE empezar con una línea que cumpla `^<!-- aucove:session (\{.*\}) -->$`, cuyo JSON valide contra `#/$defs/session_block`; los `n` DEBEN ser 1, 2, 3… sin huecos y las fechas no decrecientes (`E_VALIDATION_NOTE`).
- **REQ-SCHEMA-58.** El `login` de cada bloque DEBE existir en `people` de la base (`E_VALIDATION_UNKNOWN_PERSON`), y cada ID de "Decisiones" DEBE existir en la base o en el PR (`E_VALIDATION_UNKNOWN_DECISION`).
- **REQ-SCHEMA-59.** El núcleo DEBE agregar bloques solo al final y NO DEBE modificar bloques anteriores; si detecta que cambiaron respecto de la rama remota, DEBE emitir `W_VALIDATION_NOTE_REWRITTEN`.
- **REQ-SCHEMA-60.** Un bloque NO DEBE superar 4000 caracteres y la nota NO DEBE superar 64 KiB (`E_VALIDATION_NOTE`); una nota NO DEBE contener transcripciones de sesión.
- **REQ-SCHEMA-61.** El núcleo DEBE serializar el JSON del marcador en una sola línea y sin la secuencia `--`, que cerraría el comentario HTML. Este marcador vive en un archivo y lleva JSON; no sigue la gramática de marcadores de la forja (00-marco §5.14, 05 §6.1); es su única excepción.

  `submit` recibe el contenido del bloque como objeto `note` (05 §9.18); el agente no edita el archivo.
- **REQ-SCHEMA-86.** `submit` DEBE escribir en el front matter `ai_use` y `assisted_by` con los mismos usos y pares herramienta:modelo del objeto `trace` del PR (§9.3); la comparación la hace el check `traceability` (11-trazabilidad REQ-TRACE-40).

Qué operaciones agregan bloques (al menos `submit`) lo define 05-operaciones. Cuántos bloques entran al contexto lo define 07-hooks.

## 8. Formulario de Issue de tarea

### 8.1 `.github/ISSUE_TEMPLATE/task.yml` (versión `es`)

```yaml
# Clase `generated`: lo generan `init` y `sync-roles` desde roles.yaml; no lo edites a mano.
name: Tarea
description: Propón una tarea del proyecto. El responsable la acepta antes de que alguien la reclame.
labels: ["task", "state:proposed"]
body:
  - type: markdown
    attributes:
      value: |
        Una tarea declara las partes que puede modificar (una tarea, una rama, un PR).
        El núcleo lee este formulario: no cambies los títulos de los campos.
  - type: textarea
    id: summary
    attributes:
      label: Resumen
      description: Qué se hará y por qué, en 1 a 5 oraciones.
    validations:
      required: true
  - type: dropdown
    id: sections
    attributes:
      label: Partes
      description: |
        Partes que la tarea puede modificar.
        - `introduccion`: Introducción (@ana-perez)
        - `metodos`: Métodos (@Bruno-Diaz)
        - `resultados`: Resultados (@carla-soto)
      multiple: true
      options:
        - "introduccion"
        - "metodos"
        - "resultados"
    validations:
      required: true
  - type: input
    id: plan_refs
    attributes:
      label: Referencias al plan
      description: IDs de plan.md separados por comas. Déjalo vacío si no aplica.
      placeholder: "RQ-1, METH-1"
  - type: textarea
    id: done
    attributes:
      label: Criterio de terminado
      description: Condiciones verificables, una por línea y empezando con "- ".
      placeholder: "- La subsección 3.2 describe la limpieza de la ola 2025."
    validations:
      required: true
```

Etiquetas canónicas de los campos (el `id` no se localiza):

| `id` | Tipo | `es` | `en` | Obligatorio para el núcleo |
|---|---|---|---|---|
| `summary` | `textarea` | Resumen | Summary | sí |
| `sections` | `dropdown` múltiple (o `input`, REQ-SCHEMA-63) | Partes | Parts | no (vacío = advertencia) |
| `plan_refs` | `input` | Referencias al plan | Plan references | no |
| `done` | `textarea` | Criterio de terminado | Definition of done | sí |

- **REQ-SCHEMA-62.** `init` DEBE generar `task.yml` con estos `id`, tipos y etiquetas en el idioma del proyecto, y `sync-roles` DEBE regenerar las `options` y la lista de la `description` de `sections` con los `sections[].id` de `roles.yaml`, en orden y entre comillas. `task.yml` es de clase `generated`; `roles-sync` (09-ci) lo compara con la regeneración byte a byte tras normalizar CRLF a LF (06-permisos REQ-PERM-31).
- **REQ-SCHEMA-63.** Si `roles.yaml` no tiene secciones, `sections` DEBE generarse como `type: input` con el mismo `id` y la misma etiqueta, porque GitHub no admite un `dropdown` sin opciones.
- **REQ-SCHEMA-64.** El formulario DEBE aplicar las etiquetas `task` y `state:proposed` y NO DEBE declarar `assignees` (04-tareas REQ-TASK-42); como GitHub no agrega etiquetas que no existen, `init` DEBE crearlas antes (08-forja).
- **REQ-SCHEMA-65.** El núcleo NO DEBE confiar en `validations.required`: GitHub solo lo hace cumplir en repositorios públicos, así que todas las validaciones se repiten al analizar el cuerpo.
- **REQ-SCHEMA-66.** `.github/ISSUE_TEMPLATE/config.yml` DEBERÍA declarar `blank_issues_enabled: false`; aun así, quien tiene permiso de escritura puede abrir un Issue en blanco, y su cuerpo inválido se detecta con REQ-SCHEMA-69.

### 8.2 Cuerpo resultante

GitHub convierte cada respuesta en un encabezado `### <etiqueta>` seguido del valor. Un campo vacío aparece como `_No response_` o, en un `dropdown`, como `None`; un `dropdown` múltiple une los valores con `, `.

```markdown
### Resumen

Redactar la subsección de datos de Métodos: fuentes, período y limpieza de las
dos olas de la encuesta origen-destino.

### Partes

metodos

### Referencias al plan

RQ-1, METH-2

### Criterio de terminado

- La subsección 3.1 describe las dos olas de la encuesta.
- La subsección 3.2 explica la limpieza de la ola 2025.
- El PDF de CI compila sin referencias rotas.
```

### 8.3 Análisis del cuerpo

```python
CANON = {"Resumen": "summary", "Partes": "sections", "Referencias al plan": "plan_refs",
         "Criterio de terminado": "done", "Summary": "summary", "Parts": "sections",
         "Plan references": "plan_refs", "Definition of done": "done",
         "Secciones": "sections", "Sections": "sections"}   # etiquetas anteriores a D-43 (Issues ya creados)
EMPTY = {"_no response_", "none"}

# Problem: excepción que lleva un registro de problema (§2.4).
def parse_task_body(body: str, form_labels: dict[str, str]) -> dict:
    labels = {**CANON, **form_labels}      # form_labels: etiqueta -> id, leído de task.yml en la base
    fields, cur = {}, None
    for line in body.split("\n"):
        if re.match(r"^\s*<!--.*-->\s*$", line):   # marcadores (p. ej. aucove:op de 05-operaciones)
            continue
        m = re.match(r"^### (.+?)\s*$", line)
        if m and m[1] in labels:           # un "###" que no es etiqueta conocida es contenido
            cur = labels[m[1]]
            if cur in fields:
                raise Problem("E_VALIDATION_TASK_BODY", f"campo repetido: {cur}")
            fields[cur] = []
            continue
        if cur:
            fields[cur].append(line)
        elif line.strip():
            warn("W_VALIDATION_TASK_UNKNOWN_FIELD", line)   # texto antes del primer campo
    raw = {k: "\n".join(v).strip() for k, v in fields.items()}
    raw = {k: "" if v.lower() in EMPTY else v for k, v in raw.items()}
    spec = {
        "summary": raw.get("summary", ""),
        "sections": list(dict.fromkeys(x.strip() for x in re.split(r",\s*", raw.get("sections", "")) if x.strip())),
        "plan_refs": list(dict.fromkeys(x.upper() for x in re.split(r"[,\s]+", raw.get("plan_refs", "")) if x)),
    }
    done = raw.get("done", "")
    items = [re.sub(r"^- (\[[ xX]\] )?", "", l.strip()) for l in done.split("\n") if l.strip().startswith("- ")]
    spec["done"] = items or ([done] if done else [])
    canonical = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    spec["spec_hash"] = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return spec
```

Resultado (`TaskSpec`) para el cuerpo de §8.2, completado por el núcleo con `number` y `title` del Issue:

```json
{
  "number": 42,
  "title": "Datos y limpieza en Métodos",
  "summary": "Redactar la subsección de datos de Métodos: fuentes, período y limpieza de las\ndos olas de la encuesta origen-destino.",
  "sections": ["metodos"],
  "plan_refs": ["RQ-1", "METH-2"],
  "done": [
    "La subsección 3.1 describe las dos olas de la encuesta.",
    "La subsección 3.2 explica la limpieza de la ola 2025.",
    "El PDF de CI compila sin referencias rotas."
  ],
  "spec_hash": "sha256:c295fd3217cbf55ad3cf21ec63d6b615f55a1370f4ba9e3a007750926adb55a2"
}
```

- **REQ-SCHEMA-67.** El analizador DEBE reconocer como separadores solo los encabezados `### ` cuyo texto sea una etiqueta del `task.yml` de la base o de la tabla canónica; un encabezado repetido es `E_VALIDATION_TASK_BODY`; las líneas que son solo un comentario HTML se ignoran, y el resto del texto no vacío anterior al primer campo reconocido se descarta con `W_VALIDATION_TASK_UNKNOWN_FIELD`.
- **REQ-SCHEMA-68.** `_No response_` y `None` (sin distinguir mayúsculas) DEBEN tratarse como valor vacío.
- **REQ-SCHEMA-69.** Un `summary` vacío o de más de 2000 caracteres, o un `done` vacío o con más de 20 condiciones, DEBE producir `E_VALIDATION_TASK_BODY`.
- **REQ-SCHEMA-70.** Cada elemento de `sections` DEBE ser un `sections[].id` de la base (`E_VALIDATION_UNKNOWN_SECTION`); una lista vacía produce `W_VALIDATION_TASK_NO_SECTIONS`, y 06-permisos define qué puede tocar esa tarea.
- **REQ-SCHEMA-71.** `plan_refs` DEBE cumplir REQ-SCHEMA-44; un valor que no es un ID del plan es `E_VALIDATION_UNKNOWN_PLAN_ID`.
- **REQ-SCHEMA-72.** `spec_hash` DEBE calcularse, después de normalizar los saltos de línea (REQ-SCHEMA-1), como el SHA-256 del JSON canónico (claves ordenadas, separadores `,` y `:` sin espacios, caracteres no ASCII sin escapar, UTF-8) de `summary`, `sections`, `plan_refs` y `done`, para que 04-tareas y 06-permisos detecten ediciones del Issue posteriores a su aceptación o reclamo. 04-tareas guarda `spec_hash` y `sections` en el marcador `claim` y reporta `spec_changed_after_claim` si cambia.
- **REQ-SCHEMA-73.** `task propose` DEBE escribir el cuerpo con este mismo formato y las etiquetas del idioma del proyecto, de modo que `parse_task_body(render(spec))` devuelva el mismo `spec`.
- **REQ-SCHEMA-74.** El cuerpo de un Issue DEBE tratarse como dato no confiable: el núcleo lo analiza, nunca lo ejecuta ni lo interpola en comandos.

## 9. Plantilla de PR

### 9.1 `.github/pull_request_template.md` (versión `es`)

````markdown
<!--
Plantilla de PR (03-esquemas §9). Si entregas con `aucove submit`, el núcleo
escribe este cuerpo por ti. Si abres el PR desde la web, completa las secciones
y el bloque final sin cambiar sus marcadores ni sus claves.
-->

## Resumen

<!-- Qué cambia y por qué, en 1 a 5 oraciones. -->

## Tarea

Closes #<n>

## Cambios por parte

<!-- Una línea por parte tocada: `id`: qué cambió. -->

## Uso de IA

<!-- Herramientas, modelo y para qué se usaron. La traza formal va en el
     objeto "trace" y en los trailers (11-trazabilidad). -->

## Decisiones

<!-- IDs de los registros nuevos en memory/decisions/, o "ninguna". -->

## Verificación

- [ ] El PR solo toca las partes de la tarea, su nota y decisiones nuevas.
- [ ] La nota `memory/notes/task-<n>.md` resume el trabajo.
- [ ] Revisé el texto producido con IA y me hago responsable de él.

<!-- aucove:pr-meta-begin v1 -->
```yaml
schema: 1
task: 0
sections: []
plan_refs: []
decisions: []
```
<!-- aucove:pr-meta-end v1 -->
````

La plantilla vacía es inválida a propósito (`task: 0`): obliga a completar el bloque a quien abre el PR desde la web. Ninguna línea de la plantilla empieza con `---` (11-trazabilidad REQ-TRACE-38).

### 9.2 Final de un cuerpo escrito por `submit`

Orden final del cuerpo: … «Verificación», la línea `<!-- aucove:metrics v1 hook_denials=<n> -->` (12-mvp), el bloque pr-meta, una línea en blanco y el párrafo de trailers, que es lo último del cuerpo (11-trazabilidad REQ-TRACE-37). El objeto `trace` y los trailers los define 11-trazabilidad; aquí solo se muestra dónde van.

````markdown
<!-- aucove:metrics v1 hook_denials=0 -->
<!-- aucove:pr-meta-begin v1 -->
```yaml
schema: 1
task: 42
sections: [metodos]
plan_refs: [RQ-1, METH-2]
decisions: [2026-10-01-42-usar-panel-balanceado]
trace:                                   # objeto trace/v1 (11-trazabilidad §3.3)
  version: 1
  task: 42
  sections: [metodos]
  ai_use: [drafting, editing]
  assisted_by:
    - {tool: claude-code, model: "claude-opus-5-5", tool_source: "env:CLAUDECODE", model_source: "hook:SessionStart"}
  sign_off: {login: Bruno-Diaz, name: "Bruno Díaz", email: "bdiaz@uni.example", channel: mcp, attestation: attest-v1, at: "2026-10-03T16:20:00Z"}
  commits: {base: "9f2c1e7", head: "a1b3d44", count: 5, undeclared: 0}
  core_version: "0.1.3"
```
<!-- aucove:pr-meta-end v1 -->

Task: #42
Section: metodos
AI-Use: drafting, editing
Assisted-by: claude-code:claude-opus-5-5
Signed-off-by: Bruno Díaz <bdiaz@uni.example>
````

### 9.3 Esquema del bloque (`pr-meta/v1`)

Se publica como `schemas/pr-meta.schema.json`, con este contenido:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://aucove.invalid/schemas/pr-meta/v1",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema", "task", "sections", "plan_refs", "decisions"],
  "properties": {
    "schema": { "const": 1 },
    "task": { "type": "integer", "minimum": 1 },
    "sections": { "type": "array", "uniqueItems": true,
      "items": { "type": "string", "pattern": "^[a-z0-9]+(-[a-z0-9]+)*$" } },
    "plan_refs": { "type": "array", "uniqueItems": true,
      "items": { "type": "string", "pattern": "^(RQ|HYP|IN|OUT|METH)-[1-9][0-9]{0,3}$" } },
    "decisions": { "type": "array", "uniqueItems": true,
      "items": { "type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}-[1-9][0-9]*-[a-z0-9]+(-[a-z0-9]+)*$" } },
    "trace": { "$ref": "https://aucove.invalid/schemas/trace/v1" }
  }
}
```

El validador DEBE registrar el esquema `trace/v1` de 11-trazabilidad §3.3 para resolver el `$ref` sin red (REQ-SCHEMA-7).

### 9.4 Análisis y reglas

```python
BEGIN, END = "<!-- aucove:pr-meta-begin v1 -->", "<!-- aucove:pr-meta-end v1 -->"
PR_META = re.compile(re.escape(BEGIN) + r"[ \t]*\n```ya?ml[ \t]*\n(.*?)\n```[ \t]*\n" + re.escape(END), re.S)

def parse_pr_meta(body: str) -> dict:
    body = body.replace("\r\n", "\n")
    if body.count(BEGIN) == 0:
        raise Problem("E_VALIDATION_PR_META_MISSING")
    found = PR_META.findall(body)
    if body.count(BEGIN) != 1 or len(found) != 1:
        raise Problem("E_VALIDATION_PR_META")
    return yaml.load(found[0], Loader=StrictLoader)   # luego: esquema pr-meta/v1 y reglas de §9.4
```

- **REQ-SCHEMA-75.** Los marcadores DEBEN derivarse del nombre definido en un solo módulo (00-marco §5.1) y cumplen la gramática de 00-marco §5.14 (tipos `pr-meta-begin` y `pr-meta-end`, `v1`, sin claves).
- **REQ-SCHEMA-76.** El cuerpo de un PR de tarea DEBE contener exactamente un bloque entre `<!-- aucove:pr-meta-begin v1 -->` y `<!-- aucove:pr-meta-end v1 -->` con un solo bloque de código `yaml` (`E_VALIDATION_PR_META_MISSING` si falta; `E_VALIDATION_PR_META` si hay más de uno o está mal formado).
- **REQ-SCHEMA-77.** El bloque DEBE validar contra `pr-meta/v1`, y `task` DEBE ser el `<n>` de la rama `task/<n>` del PR (`E_VALIDATION_PR_META`).
- **REQ-SCHEMA-78.** `decisions` DEBE ser igual al conjunto de IDs de los registros que el PR agrega en `memory/decisions/` (`E_VALIDATION_PR_META`).
- **REQ-SCHEMA-79.** `sections` DEBE ser igual al conjunto de secciones cuyos archivos toca el PR, calculado por el check de alcance de 09-ci (`E_VALIDATION_PR_META`).
- **REQ-SCHEMA-87.** Si el bloque tiene `trace`, `trace.task` y `trace.sections` (cuando existen) DEBEN coincidir con `task` y `sections` (`E_VALIDATION_PR_META`); si `trace` falta o no es válido, lo informa el check `traceability` de 11-trazabilidad.
- **REQ-SCHEMA-80.** `submit` DEBE escribir el cuerpo completo desde la plantilla localizada, con `Closes #<n>`, el bloque con `trace` y, al final, el párrafo de trailers (11-trazabilidad), y reescribirlo en cada entrega de forma idempotente: con las mismas entradas produce el mismo cuerpo. `submit` DEBE serializar el bloque con `yaml.safe_dump(..., allow_unicode=True, sort_keys=False, default_flow_style=None)` y escribir siempre entre comillas dobles `commits.base`, `commits.head`, `core_version`, `model`, `email` y `at`.
- **REQ-SCHEMA-88.** El analizador DEBE ignorar los demás marcadores `aucove:` del cuerpo, como el de métricas de 12-mvp (`<!-- aucove:metrics v1 hook_denials=<n> -->`), que va en la posición que fija el orden de §9.2.
- **REQ-SCHEMA-81.** El bloque es una declaración y NO DEBE usarse para autorizar: autorizan `roles.yaml` de la base, el Issue y el diff (06-permisos).
- **REQ-SCHEMA-82.** El CI DEBE leer el cuerpo del PR como dato (por la API o un archivo) y NO DEBE interpolarlo en comandos de shell, porque `github.event.pull_request.body` es entrada no confiable (09-ci).

## 10. Catálogo de problemas de este capítulo (propuesto)

Son códigos de hallazgo (00-marco §5.11); no entran en el catálogo cerrado de 05. Fuera de `validate`, un archivo inválido hace fallar la operación con `E_CONFIG_INVALID` y `details.code`.

| Código | Severidad | Cuándo |
|---|---|---|
| `E_CONFIG_NOT_FOUND` | error | falta `roles.yaml` (si falta `project.yaml`, la operación falla con el error de operación `E_CONFIG_NOT_A_PROJECT`) |
| `E_CONFIG_YAML` | error | sintaxis YAML, clave duplicada, alias o clave no textual (REQ-SCHEMA-2) |
| `E_CONFIG_CORE_MISMATCH` | error | la versión del núcleo no satisface `core` |
| `E_CONFIG_CONVENTION_UNSUPPORTED` | error | el núcleo no admite `convention` |
| `E_CONFIG_REMOTE_MISMATCH` | error | `forge.repo` distinto del repo del CI |
| `E_CONFIG_BASE_NOT_DEFAULT` | error | REQ-SCHEMA-19 |
| `E_CONFIG_PATH_NOT_FOUND` | error | falta `build.main` o un `bib_files[]` |
| `E_VALIDATION_SCHEMA` | error | violación de un JSON Schema |
| `E_VALIDATION_FRONT_MATTER` | error | front matter ausente, mal delimitado o que no es un mapa |
| `E_VALIDATION_ROLES_LEAD` | error | REQ-SCHEMA-24 |
| `E_VALIDATION_ROLES_DEPUTY` | error | REQ-SCHEMA-25 |
| `E_VALIDATION_ROLES_MODE` | error | REQ-SCHEMA-94 |
| `E_VALIDATION_DUPLICATE_LOGIN` | error | REQ-SCHEMA-26 |
| `E_VALIDATION_DUPLICATE_ID` | error | sección o ID del plan repetidos |
| `E_VALIDATION_UNKNOWN_PERSON` | error | login que no está en `people` |
| `E_VALIDATION_INACTIVE_PERSON` | error | persona con `active: false` en un rol que exige actividad |
| `E_VALIDATION_ORCID` | error | dígito de control inválido |
| `E_VALIDATION_PLAN_SYNTAX` | error | REQ-SCHEMA-36 |
| `E_VALIDATION_PLAN_ID_REMOVED` | error | REQ-SCHEMA-40 |
| `E_VALIDATION_PLAN_RETIRED_CHANGED` | error | REQ-SCHEMA-41 |
| `E_VALIDATION_PLAN_ID_REUSED` | error | REQ-SCHEMA-42 |
| `E_VALIDATION_UNKNOWN_PLAN_ID` | error | ID del plan inexistente |
| `E_VALIDATION_UNKNOWN_DECISION` | error | ID de decisión inexistente |
| `E_VALIDATION_UNKNOWN_SECTION` | error | sección inexistente en un Issue de tarea |
| `E_VALIDATION_DECISION_NAME` | error | REQ-SCHEMA-45, 46 y 48 |
| `E_VALIDATION_DECISION_SUPERSEDES` | error | REQ-SCHEMA-52 |
| `E_VALIDATION_DECISION_BODY` | error | REQ-SCHEMA-53 |
| `E_VALIDATION_NOTE` | error | REQ-SCHEMA-56, 57 y 60 |
| `E_VALIDATION_TASK_BODY` | error | REQ-SCHEMA-67 y 69 |
| `E_VALIDATION_PR_META_MISSING` | error | REQ-SCHEMA-76 |
| `E_VALIDATION_PR_META` | error | REQ-SCHEMA-76 a 79 |
| `W_CONFIG_REMOTE_MISMATCH` | warning | REQ-SCHEMA-18, en local |
| `W_VALIDATION_LOGIN_CASE` | warning | REQ-SCHEMA-30 |
| `W_VALIDATION_PLAN_TEXT_CHANGED` | warning | REQ-SCHEMA-43 |
| `W_VALIDATION_PLAN_REF_RETIRED` | warning | REQ-SCHEMA-44 |
| `W_VALIDATION_DECISION_AUTHOR` | warning | REQ-SCHEMA-54 |
| `W_VALIDATION_NOTE_REWRITTEN` | warning | REQ-SCHEMA-59 |
| `W_VALIDATION_TASK_NO_SECTIONS` | warning | REQ-SCHEMA-70 |
| `W_VALIDATION_TASK_UNKNOWN_FIELD` | warning | REQ-SCHEMA-67: texto antes del primer campo reconocido |

## 11. Identificadores de este capítulo

| Identificador | Tipo | Definición |
|---|---|---|
| Claves de `project.yaml` (§3.2) | claves YAML | `convention`, `core`, `core_pin`, `name`, `title`, `language`, `format`, `disclosure`, `forge.{type,repo,base_branch,advisory_acceptance}`, `build.{engine,main,image,bibliography,bib_files,shell_escape,compile_on_submit,timeout_minutes}`, `limits.{max_active_claims,claim_expiry_days,expiry_warning_days,handoff_hold_days,exclusive_sections}`, `context.{max_chars,recent_decisions}`, `coordinator.require_direction_ack`, `review.sla_hours`, `data_policy.{level,require_commercial,human_subjects,ethics_ref}`, `licenses.{holder,text,code,data}`, `evidence_signing.{mode,keys[].{id,public_key,since,until}}` (D-72), `ci.{pdf_retention_days,latexdiff,model_review.{enabled,auth,model,max_turns}}` |
| Valores de `project.yaml` | enumeraciones | `none` de `format` (D-43); `private`, `milestones`, `open` de `disclosure` (D-44); `pdflatex`, `xelatex`, `lualatex`; `biber`, `bibtex`, `none`; `disabled`, `restricted`; `never`, `if_available`, `always`; `public`, `internal`; `api_key`, `oidc`; `ci`, `admin`, `none` de `evidence_signing.mode` (D-72) |
| Claves y valores reservados | claves YAML | `forge.host`, `format: markdown\|quarto`, `data_policy.level: restricted\|local_only`, `ci.model_review.{provider,environment,trigger}`, `evidence` |
| Claves de `roles.yaml` (§4.2) | claves YAML | `version`, `lead`, `deputy`, `people[].{login,forge_id,name,email,orcid,role,account_type,credit,active}`, `sections[].{id,title,owner}` |
| `account_type` | enumeración | `consumer_training_on`, `consumer_training_off`, `team`, `enterprise`, `api`, `cloud_provider`, `local`, `none`, `unknown` (§4.4; `roles.schema.json`) |
| `credit` | enumeración | los 14 identificadores de §4.3 |
| `(retired)`, `(retired: <id>)` | marcador en `plan.md` | elemento retirado (§5.3) |
| Front matter de decisión | claves | `id`, `title`, `date`, `author`, `origin`, `task`, `status`, `summary`, `supersedes`, `plan_refs` |
| `agent`, `human` | valores de `origin` | quién creó el registro (§6.2) |
| `accepted`, `rejected` | valores de `status` | sentido del registro (§6.2) |
| `pending`, `superseded` | estados efectivos | calculados, nunca escritos (§6.5) |
| Front matter de nota | claves | `task`, `created`, `created_by`, `ai_use`, `assisted_by` |
| `aucove:session` | marcador HTML | abre un bloque de sesión; claves `n`, `date`, `login`, `door`, `model` |
| Valores de `door` | recomendados | `claude-code`, `codex`, `antigravity-cli`, `gemini-cli`, `opencode`, `goose`, `forge-web`, `manual` |
| `summary`, `sections`, `plan_refs`, `done` | `id` de campos del formulario | §8.1 |
| `TaskSpec`, `spec_hash` | objeto y campo | resultado de `parse_task_body` (§8.3) |
| `aucove:pr-meta-begin`, `aucove:pr-meta-end` (`v1`) | marcadores HTML | delimitan el bloque del PR (00-marco §5.14) |
| `https://aucove.invalid/schemas/pr-meta/v1` | `$id` | esquema del bloque (§9.3), publicado como `schemas/pr-meta.schema.json` |
| Claves del bloque del PR | claves | `schema`, `task`, `sections`, `plan_refs`, `decisions`, `trace` |
| `https://aucove.invalid/schemas/evidence-manifest/v1`, `evidence-openings/v1`, `evidence-record/v1`, `evidence-signature/v1` | `$id` | esquemas de evidencia que se empaquetan con el núcleo (REQ-SCHEMA-93); semántica en 11-trazabilidad §8 |
| REQ-SCHEMA-90 a REQ-SCHEMA-93, REQ-SCHEMA-96 | requisitos | `format: none` sin `build`, `require_commercial` efectivo, `disclosure: open` con datos públicos, esquemas de evidencia y llaves del proyecto (§3.3) |
| Códigos de §10 | códigos de hallazgo | nuevos en las áreas `CONFIG` y `VALIDATION` (00-marco §5.11) |

## 12. Supuestos sobre otros capítulos

| Capítulo | Supuesto |
|---|---|
| 01-arquitectura | Define la regla de compatibilidad `convention` ↔ núcleo, empaqueta los esquemas y localiza las etiquetas del formulario, la plantilla de PR y los encabezados de notas y decisiones. |
| 02-convencion | Cada parte vive en `sections/<id>/`; `memory/decisions/` y `memory/notes/` solo contienen registros, notas y un `.gitkeep` (sin `README.md` ni `_plantilla.md`); el `.bib` por defecto es `bib/references.bib`. |
| 04-tareas | Un Issue nuevo nace con `state:proposed` (lo pone el formulario) y `task accept` la quita; las secciones de la tarea salen de `TaskSpec.sections`; `spec_hash` sirve para detectar ediciones tras aceptar o reclamar; el cambio de dirección usa los IDs del plan y `W_VALIDATION_PLAN_TEXT_CHANGED`; todo registro de decisión se agrega desde una rama `task/<n>`. |
| 05-operaciones | `validate_project` entrega la lista de problemas de §2.4 dentro de `data`; los códigos de §10 son de hallazgo y no entran en su catálogo cerrado; `submit` recibe `note` como objeto; `decision propose` recibe `summary`, `consequences` y `status`; `task propose` y `decision propose` usan §8.3 y §6.1. |
| 06-permisos | Define qué puede tocar una tarea sin secciones, y cómo se combinan las secciones declaradas con la propiedad; no confía en el cuerpo del Issue editado después de aceptarlo. |
| 07-hooks | El paquete de contexto respeta `context.max_chars` ≤ 9000, usa `summary` de las decisiones vigentes y carga solo los últimos bloques de la nota de la propia tarea, marcados como no confiables. |
| 08-forja | Obtiene el `login` autenticado, crea las etiquetas en `init`, lee Issues y PR por REST, identifica el autor del PR y la rama por defecto. |
| 09-ci | Ejecuta la validación de este capítulo en cada PR, el check de inmutabilidad de decisiones, el cálculo de secciones tocadas (REQ-SCHEMA-79) y el mapeo `build.engine` → latexmk (`-pdf`, `-pdfxe`, `-pdflua`). |
| 10-seguridad | Fija el orden de protección de `account_type` (REQ-SCHEMA-22); `unknown` solo se admite con `data_policy.level: public`; regula la publicación de `email` en `roles.yaml`. |
| 11-trazabilidad | Define el objeto `trace/v1` del bloque del PR y sus claves (11-trazabilidad §3.3); define en inglés ASCII el vocabulario de `AI-Use` (que usan `trace.ai_use` y `ai_use` de la nota; en los ejemplos: `drafting`, `editing`) y el formato de `Assisted-by` (`trace.assisted_by` y `assisted_by` de la nota); construye el libro de aprobaciones desde el merge. |

## 13. Preguntas abiertas

- **Q-SCHEMA-2.** ¿Se agrega el grado de contribución de CRediT (`lead`, `equal`, `supporting`) a `people[].credit` en `roles/v2`?
- **Q-SCHEMA-3.** ¿Se admiten logins antiguos de GitHub que no cumplen las reglas actuales (por ejemplo, con guion final)? El patrón actual los rechaza.
- **Q-SCHEMA-4.** Con muchas secciones (más de 30), ¿sigue siendo usable el `dropdown`, o conviene un `input` libre validado por el núcleo?
- **Q-SCHEMA-5.** D-06, hoy reemplazada por D-43, pedía que las rutas se configuraran en `project.yaml`. v0.1 solo expone `build.main` y `build.bib_files`, y D-43 fija las partes en `sections/<id>/` para cualquier formato; ¿se agrega `paths.sections` al abrir Markdown o Quarto?
- **Q-SCHEMA-6.** ¿Fecha local o UTC en el nombre de la decisión? Se eligió la local (REQ-SCHEMA-49) porque no afecta la unicidad.
- **Q-SCHEMA-7.** ¿Debe `account_type` ser una lista por herramienta (por ejemplo Claude Team y Codex personal) en lugar de un solo valor?
- **Q-SCHEMA-8.** ¿Necesita `plan.md` un prefijo para el objetivo (p. ej. `GOAL-`)? El registro de 00-marco §5.9 no lo tiene.
- **Q-SCHEMA-9.** ¿Debe `ai_use` declararse por sección en lugar de por PR?

## 14. Referencias

Verificadas el 2026-09-29.

- GitHub, sintaxis de issue forms (ubicación, claves, etiquetas inexistentes): https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-issue-forms
- GitHub, esquema de formularios (`required` "Only for public repositories", `markdown` no se envía, `id` alfanumérico con `-` y `_`): https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-githubs-form-schema
- GitHub, errores de validación de formularios ("None" reservado, etiquetas e IDs únicos): https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/common-validation-errors-when-creating-issue-forms
- GitHub, `config.yml` y `blank_issues_enabled`: https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/configuring-issue-templates-for-your-repository
- Formato del cuerpo de un issue form (`### <etiqueta>`, `_No response_`, `None`, separador `, `): biblioteca oficial `github/issue-parser`, https://github.com/github/issue-parser (la documentación de GitHub no lo detalla; **supuesto** respaldado por esa biblioteca).
- GitHub, plantilla de PR (`.github/pull_request_template.md`, rama por defecto): https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/creating-a-pull-request-template-for-your-repository
- GitHub, palabras clave de enlace (`Closes #n`, solo hacia la rama por defecto): https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue
- GitHub REST, PR asociados a un commit: https://docs.github.com/en/rest/commits/commits#list-pull-requests-associated-with-a-commit
- GitHub, nombres de usuario (alfanuméricos y guiones, ≤ 39, sufijo `_shortcode` en EMU): https://docs.github.com/en/enterprise-cloud@latest/admin/managing-iam/iam-configuration-reference/username-considerations-for-external-authentication
- GitHub Actions, entradas no confiables (`github.event.pull_request.body`): https://docs.github.com/en/actions/concepts/security/script-injections
- CRediT, 14 roles y URIs: https://credit.niso.org/
- CRediT, implementación (el grado de contribución no es parte de ANSI/NISO Z39.104-2022): https://credit.niso.org/implementing-credit/
- ORCID, estructura y dígito de control MOD 11-2 (página de soporte con acceso bloqueado al verificar; datos confirmados por búsqueda): https://support.orcid.org/hc/articles/360006897674
- JSON Schema 2020-12, `format` como anotación, `minContains`/`maxContains`, `date`: https://json-schema.org/draft/2020-12/json-schema-validation
- Python Packaging, especificadores de versión (PEP 440): https://packaging.python.org/en/latest/specifications/version-specifiers/
- Claude Code, tope de 10 000 caracteres de `additionalContext`: https://code.claude.com/docs/en/hooks
- Claude Code, términos de consumo (Free, Pro, Max) frente a comerciales (Team, Enterprise, API, nube): https://code.claude.com/docs/en/data-usage
- Claude Code GitHub Actions (proveedores, OIDC, `claude_args --model`): https://code.claude.com/docs/en/github-actions
- latexmk, opciones `-pdf`, `-pdfxe`, `-pdflua`: https://mirrors.ctan.org/support/latexmk/latexmk.txt
- **Supuestos no verificados en documentación oficial:** nombres de repositorio con `[A-Za-z0-9._-]` y hasta 100 caracteres; caracteres admitidos en nombres de environment de GitHub; que el formulario aplique sus etiquetas aunque el autor no tenga permiso de triage.
