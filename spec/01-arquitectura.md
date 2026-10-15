# 01 — Arquitectura

- **Código:** `ARCH` · **Estado:** v0.1
- **Autoridad (00-marco §6):** estructura del repositorio del framework; módulos del paquete `aucove`; dependencias; distribución e instalación; versionado y compatibilidad convención ↔ núcleo; presupuestos de rendimiento y caché local; comportamiento sin red; i18n; generación de `.claude/skills/` y `.agents/skills/` desde una sola fuente; observabilidad local.
- **Solo referencia (no redefine):** sobre de resultado, catálogo de errores y códigos de salida (05-operaciones); contrato de `aucove hook <evento>` y contenido de `.claude/settings.json` (07-hooks); contenido de `CODEOWNERS` y reglas deny (06-permisos); esquemas (03-esquemas); árbol de la plantilla (02-convencion); adaptador de forja, identidad y sesiones en la nube (08-forja); workflows del proyecto (09-ci); firma y cadena de suministro (10-seguridad); estrategia de pruebas (12-mvp).

## 1. Principios

1. **Una sola lógica.** Cada operación del registro (00-marco §5.7) tiene una única implementación en `aucove.ops`. La CLI, el servidor MCP y los hooks son envolturas delgadas (D-01).
2. **La ruta de hooks es caliente.** Corre en cada llamada a herramienta: sin red, sin dependencias pesadas y, ante cualquier duda, cerrada (D-13).
3. **Una sola fuente por artefacto.** Skills, plantilla y esquemas se escriben una vez y se generan de forma determinista (D-05).
4. **Identificadores en inglés ASCII; texto localizado** (D-02).
5. **El nombre en un solo lugar** (D-03, 00-marco §5.1).

## 2. Repositorio del framework

### 2.1 Árbol

```
aucove/                      # repositorio del framework (Apache-2.0, D-07)
  pyproject.toml               # nombre de distribución y script (D-03)
  uv.lock                      # versiones exactas para desarrollo y CI del framework
  README.md LICENSE NOTICE CHANGELOG.md
  CONTRIBUTING.md              # DCO obligatorio (D-07)
  CODE_OF_CONDUCT.md SECURITY.md
  src/aucove/                # paquete importable (§3)
  skills/                      # FUENTE ÚNICA de skills (§10)
    LICENSE                    # MIT-0 (REQ-TRACE-57)
    <key>/skill.toml
    <key>/body.es.md
    <key>/body.en.md
  template/                    # FUENTE del repo plantilla (§2.3, §10.5)
    base/                      # archivos comunes a todos los idiomas
    l10n/es/  l10n/en/         # variantes localizadas (mismo conjunto de rutas)
  schemas/                     # *.schema.json publicados (contenido: 03-esquemas y 05-operaciones)
  spec/                        # esta especificación (CC-BY-4.0)
  docs/es/  docs/en/           # documentación de usuario (CC-BY-4.0)
  docs/acceptance/v<X.Y.Z>.md  # informe de aceptación de cada versión (REQ-MVP-15)
  governance/es/ governance/en/  # plantillas institucionales: política de IA, clasificación de datos (CC0-1.0)
  deploy/README.md             # RESERVADO (modo completo, D-16); sin código en v0.1
  tools/                       # scripts de build y release; van en el sdist, no en la rueda
  tests/
    unit/ render/ hooks/ perf/ contract/ race/ mcp/ skills/ e2e/ model/ fixtures/
  evals/
    coordinator/cases/         # conjunto de referencia del coordinador (12-mvp)
  .github/workflows/ci.yml release.yml nightly.yml
```

- **REQ-ARCH-1.** `skills/` DEBE ser el único lugar del repositorio del framework donde se escribe el contenido de un skill; ningún `SKILL.md` DEBE versionarse en otra ruta del framework.
- **REQ-ARCH-2.** `template/` DEBE ser la única fuente del repositorio plantilla publicado; el repositorio plantilla NO DEBE editarse a mano (§2.3).
- **REQ-ARCH-3.** `skills/`, `template/` y `schemas/` DEBEN empaquetarse dentro de la rueda en `aucove/_data/{skills,template,schemas}`, y `tools/template_vars.toml` en `aucove/_data/template_vars.toml`, para que `init` (también `init --repo`, 00-marco D-73) y `validate` funcionen sin red.
- **REQ-ARCH-4.** `deploy/` NO DEBE contener código ejecutable en v0.1; solo un `README.md` que declare el directorio reservado.
- **REQ-ARCH-5.** `tests/` DEBE seguir la división del árbol (`unit`, `render`, `hooks`, `perf`, `contract`, `race`, `mcp`, `skills`, `e2e`, `model`, `fixtures`); los casos del coordinador DEBEN estar en `evals/coordinator/cases/` y el informe de aceptación de cada versión en `docs/acceptance/v<X.Y.Z>.md` (REQ-MVP-15). Los casos y los criterios de aceptación los define 12-mvp. Marcadores de pytest: `live` (forja real, 08-forja) y `model`.
- **REQ-ARCH-6.** El CI del propio framework (`.github/workflows/ci.yml`) DEBE ejecutar, en Linux, macOS y Windows con Python 3.11 y con la versión más reciente soportada: pruebas unitarias, contratos de importación (§3.5), render en modo `--check` (§10), comprobaciones de i18n (§9.4) y la prueba de latencia de §7.3.

### 2.2 Licencias por directorio (según D-07)

| Directorio | Licencia | Motivo |
|---|---|---|
| `src/`, `tools/`, `tests/` | Apache-2.0 | código del framework |
| `template/` | MIT-0 (código) y CC0-1.0 (texto de ejemplo), con sus propios `LICENSE` que viajan al repo plantilla | no imponer licencia a la investigación |
| `docs/`, `spec/` | CC-BY-4.0 | documentación |
| `governance/` | CC0-1.0 | plantillas institucionales que cada institución adapta sin atribución (D-07) |
| `skills/` | MIT-0 (REQ-TRACE-57); cada `SKILL.md` generado lleva `license: MIT-0` en el frontmatter (§10.3) | se copian a cada proyecto, igual que la plantilla |

### 2.3 Repositorio plantilla publicado

- **Nombre:** `aucove-template`, en la misma organización que el framework (nombre derivado de `_meta.NAME`; ver §12).
- **Contenido:** exactamente `render_template(template/, language="en", core_pin=<versión liberada>, framework_repo=<org del framework>/aucove)` (§10.5), con `core` derivado de esa versión como lo escribe `init` (REQ-ARCH-44), para que el pin lo cumpla aunque sea una versión preliminar, incluidos los archivos generados de `.claude/skills/`, `.agents/skills/` y la base de `.claude/settings.json`. `framework_repo` es el repositorio del que los workflows de la plantilla bajan `requirements-lock.txt` (09-ci REQ-CI-12; 10-seguridad REQ-SEC-45). Su valor por defecto en el framework es un marcador con un dueño que GitHub no admite, para que la descarga falle en cerrado; la plantilla NO DEBE publicarse con ese marcador ni con un dueño de ejemplo que exista, porque ese dueño decidiría qué dependencias instala el CI de cada proyecto.
- **Historia:** un commit por versión liberada, con mensaje `aucove vX.Y.Z` y `Signed-off-by`, y una etiqueta anotada `vX.Y.Z`. GitHub crea cada proyecto nuevo con un solo commit y sin historia compartida con la plantilla ("starts with a single commit"; "unrelated histories"), por lo que las mejoras no llegan solas a los proyectos existentes (§10.7).

Flujo de `release.yml`, disparado por una etiqueta `vX.Y.Z` en la rama principal del framework:

```
job ci           : la matriz completa de ci.yml sobre el commit etiquetado (workflow_call)
job live         : nightly.yml: las pruebas live, antes de cada versión (08-forja REQ-FORGE-64)
job build        : needs ci, live
                   python tools/release.py check-tag --tag vX.Y.Z   (la etiqueta es la versión del paquete; dice si es preliminar)
                   python tools/release.py pypi-readme --repo OWNER/REPO --ref vX.Y.Z   (el README es la descripción de PyPI: sus rutas relativas, absolutas a la etiqueta)
                   uv build --build-constraints <grupo dev de uv.lock> --require-hashes -> dist/*.whl, dist/*.tar.gz
                   uv export --frozen --no-dev --no-emit-project --extra evidence --format requirements.txt -o assets/requirements-lock.txt
                   uv export --frozen --no-dev --no-emit-project --extra evidence --format cyclonedx1.5 -o assets/sbom.cdx.json
                   python tools/release.py sbom-version   (la versión es dinámica: uv export deja vacía metadata.component.version)
                   python tools/render_template.py --lang en --core-pin X.Y.Z --out build/template
job pypi         : needs build ; environment "pypi" ; permissions: id-token: write, attestations: write
                   actions/attest   subject-path: dist/*                                   (procedencia; 10-seguridad §6.2)
                   actions/attest   subject-path: dist/*   sbom-path: assets/sbom.cdx.json   (SBOM)
                   pypa/gh-action-pypi-publish   (Trusted Publishing; atestaciones PEP 740 por defecto)
job template     : needs build, pypi ; solo si X.Y.Z no es preliminar ; environment "template-publish"
                   token de GitHub App instalada SOLO en aucove-template,
                     permisos: contents:write, workflows:write
                   GET /repos/{owner}/aucove-template    -> si is_template es false, fallar
                   clonar aucove-template ; reemplazar todo el árbol salvo .git por build/template
                   git commit -s -m "aucove vX.Y.Z"
                   git tag -a vX.Y.Z -m "aucove vX.Y.Z"
                   git push --atomic origin HEAD:refs/heads/main refs/tags/vX.Y.Z
                   git ls-remote --tags origin vX.Y.Z      -> debe devolver una línea; si no, fallar
job gh-release   : needs template (o template omitido) ; notas desde CHANGELOG.md (tools/release.py notes),
                   con enlace a https://github.com/{owner}/aucove-template/compare/v<anterior>...vX.Y.Z
                   en una versión final ; preliminar marcada como tal
                   adjunta assets/requirements-lock.txt y assets/sbom.cdx.json
```

- **REQ-ARCH-7.** El job `template` DEBE correr solo después de que la versión `X.Y.Z` esté publicada en PyPI, para que el `core_pin` de la plantilla siempre sea instalable.
- **REQ-ARCH-8.** Una versión preliminar (`X.Y.ZrcN`, `aN`, `bN`) PUEDE publicarse en PyPI, pero NO DEBE actualizar la rama principal de `aucove-template`.
- **REQ-ARCH-9.** El release DEBE verificar con GET que `aucove-template` tiene `is_template: true` y fallar si no; se marca una vez a mano.
- **REQ-ARCH-10.** El token del job `template` DEBE tener el permiso `workflows`, porque el árbol incluye `.github/workflows/**`; sin él, GitHub rechaza el push.
- **REQ-ARCH-11.** `nightly.yml` DEBE comprobar que el árbol de la rama principal de `aucove-template` es idéntico, byte a byte tras normalizar CRLF a LF, al render de la última etiqueta estable.
- **REQ-ARCH-12.** La plantilla NO DEBE contener archivos en Git LFS, porque GitHub no los admite en repositorios plantilla.

## 3. Módulos del paquete `src/aucove/`

### 3.1 Tabla de módulos

| Módulo | Responsabilidad | Puede importar | Ruta caliente de hooks |
|---|---|---|---|
| `_meta` | `NAME`, `__version__`, `SUPPORTED_CONVENTIONS`, derivados del nombre (§3.2) | nada | sí |
| `entry` | punto de entrada del script; despacha por el primer argumento (§3.3) | `_meta` | sí |
| `domain` | tipos y funciones puras: clases de ruta, normalización de rutas, estado calculado de tarea (reglas en 04-tareas), `ascii_slug` (§9.5), compatibilidad (§6.3) | solo stdlib | sí |
| `config` | carga de `project.yaml` y `roles.yaml` desde la rama base (D-08), variables `AUCOVE_*`, validación con esquemas | `domain`, `vcs`, `i18n`; `yaml`, `jsonschema` y `packaging` con import perezoso | solo lectura de caché |
| `vcs` | acceso al clon local: lectura directa de `HEAD` y refs; `git` por `subprocess` | `domain` | sí, solo lectura de archivos |
| `localstore` | directorio `.aucove/`: caché, escritura atómica, invalidación (§7.4) | `domain` | sí |
| `obs` | registro de eventos local (§11) | `localstore` | sí |
| `i18n` | catálogos `es`/`en` y función `t()` (§9) | stdlib (`tomllib`) | sí |
| `forge` | `base.py` (interfaz, definida en 08-forja), `github.py` (REST), `fake.py` (en memoria, con inyección de fallas) | `domain`; `httpx2` y `truststore` solo desde `github.py` | no |
| `render` | skills, plantilla, `CODEOWNERS`, base de `.claude/settings.json` y reglas de `settings.local.json` (contenidos definidos en 06 y 07) | `domain`, `config`, `i18n` | no |
| `evidence` | manifiesto canónico, compromisos de archivos y actores, aperturas, registros, catálogo de TSA (`_data/tsa-catalog.json`), clientes RFC 3161 y OpenTimestamps, OCSP/CRL, la firma del proyecto (SSHSIG) y verificación (11-trazabilidad §8) | `domain`, `vcs`, `i18n`; `rfc3161_client`, `opentimestamps`, `cryptography` y `httpx2` con importación perezosa y solo con el extra `evidence` | no |
| `ops` | una implementación por operación y el registro `ops.registry` (§3.4) | `domain`, `config`, `vcs`, `localstore`, `obs`, `i18n`, `render`, `evidence`, interfaz de `forge` | solo `ops.check_path` y `ops.context` |
| `cli` | parser generado del registro, confirmación TTY de operaciones H, salida humana; `cli.ci`, subcomandos internos `aucove ci` (contrato: 09-ci) | `ops`, `config`, `i18n`, `obs` | no |
| `mcp` | servidor stdio; registra herramientas desde `ops.registry` | `ops`, `i18n`, `obs`, SDK `mcp` | no |
| `hooks` | implementa el contrato de 07-hooks: lee JSON de stdin, llama a `ops.check_path`/`ops.context`, escribe la respuesta | `ops` (las dos ligeras), `domain`, `config`, `vcs`, `localstore`, `obs`, `i18n` | sí |

Diagrama de dependencias (una flecha significa "importa"; `domain` lo importan todos y se omite):

```
                          entry
            +---------------+----------------+
            v               v                v
           cli             mcp             hooks
            |               |                |
            +-------+-------+                |  (solo ops.check_path, ops.context)
                    v                        v
                   ops ----------------------+
    +--------+------+-------+--------+--------+-------+
    v        v      v       v        v        v       v
  forge   render  config   vcs   localstore  obs    i18n
  (base)    |       |                 ^       |
    ^       +-> config, i18n          +-------+
    |
  forge.github / forge.fake  <- elegido por ops.context_factory con import perezoso
```

- **REQ-ARCH-13.** `cli`, `mcp` y `hooks` NO DEBEN contener lógica de negocio: toda regla de permisos, estado o forja DEBE vivir en `ops` o en `domain`.
- **REQ-ARCH-14.** Ningún módulo, salvo `entry`, DEBE importar `cli`, `mcp` o `hooks`.
- **REQ-ARCH-15.** `ops` NO DEBE importar `forge.github` ni `forge.fake` directamente; la implementación DEBE elegirse en una fábrica según `project.yaml: forge.type` (03-esquemas), con import dentro de la función.
- **REQ-ARCH-16.** Solo `aucove.mcp` DEBE importar el SDK MCP, de modo que cambiar de SDK (por ejemplo, a FastMCP) solo toque ese módulo.
- **REQ-ARCH-17.** `ops` DEBE acceder al clon local solo a través de `vcs`. En v0.1 existe una única implementación (clon local); la variante sobre API remota para el modo completo queda reservada.
- **REQ-ARCH-18.** `domain` NO DEBE hacer E/S (archivos, red, subprocesos ni reloj); el tiempo se recibe como argumento.

### 3.2 Nombre

```python
# src/aucove/_meta.py  -- el ÚNICO módulo con el nombre literal (D-03)
NAME = "aucove"
__version__ = "0.1.0"                  # hatch lee este valor ([tool.hatch.version] path)
SUPPORTED_CONVENTIONS = ("0.1",)
MCP_SERVER_NAME = NAME                  # Claude Code: mcp__aucove__<tool>
CACHE_DIR = "." + NAME                  # .aucove/
ENV_PREFIX = NAME.upper() + "_"         # AUCOVE_
SCHEMA_BASE = f"https://{NAME}.invalid/schemas"
TEMPLATE_REPO = NAME + "-template"
```

- **REQ-ARCH-19.** El literal del nombre DEBE aparecer solo en `_meta.py`, en `pyproject.toml` y en el nombre del directorio del paquete; una prueba DEBE fallar si aparece en otro archivo de `src/` (excepto catálogos y datos generados a partir de `_meta`).
- **REQ-ARCH-20.** La versión DEBE leerse de `_meta.__version__`; el código NO DEBE usar `importlib.metadata` en tiempo de ejecución, porque recorre los metadatos instalados y encarece el arranque.

### 3.3 Punto de entrada y despacho

```python
# src/aucove/entry.py  -- solo stdlib en el nivel superior
import sys

def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    head = argv[0] if argv else ""
    if head == "hook":                       # ruta caliente: sin parser de CLI ni red
        from aucove.hooks.main import run
        return run(argv[1:])
    if head == "mcp":
        from aucove.mcp.server import run
        return run(argv[1:])
    if head == "ci":                         # subcomandos internos del CI (contrato: 09-ci); fuera de REGISTRY
        from aucove.cli.ci import run
        return run(argv[1:])
    from aucove.cli.main import run
    return run(argv)
```

`pyproject.toml` declara `[project.scripts] aucove = "aucove.entry:main"`.

### 3.4 Registro de operaciones: CLI y MCP como envolturas

```python
# src/aucove/ops/registry.py
from dataclasses import dataclass
from typing import Literal

@dataclass(frozen=True, slots=True)
class Param:
    name: str
    type: type
    required: bool = False

@dataclass(frozen=True, slots=True)
class OpSpec:
    cli: tuple[str, ...]            # ("claim",) o ("task", "propose")
    mcp_tool: str | None            # "claim_task"; None si no se expone (00-marco §5.7)
    human_only: bool                # H de 00-marco §5.7 (D-09)
    offline: Literal["local", "degradada", "requiere"]   # columna «Sin red» de 05-operaciones §9.1 (§8.2)
    module: str                     # "aucove.ops.claim" (import perezoso)
    params: tuple[Param, ...] = ()

REGISTRY: tuple[OpSpec, ...] = (
    OpSpec(("claim",), "claim_task", False, "requiere", "aucove.ops.claim", (Param("n", int, True),)),
    OpSpec(("task", "accept"), None, True, "requiere", "aucove.ops.task_accept", (Param("n", int, True),)),
    OpSpec(("check-path",), "check_path", False, "local", "aucove.ops.check_path", (Param("path", str, True),)),
    # ... una entrada por cada fila "sí" de 00-marco §5.7
)

def invoke(spec: OpSpec, ctx: "OpContext", **kwargs) -> "Result":
    import importlib
    if spec.human_only and not ctx.human_confirmed:
        return Result.error(area="HUMAN_ONLY", ...)   # código concreto: 05-operaciones
    module = importlib.import_module(spec.module)
    return module.run(ctx, **kwargs)                 # devuelve el sobre de 05-operaciones
```

- **REQ-ARCH-21.** El parser de la CLI y la lista de herramientas MCP DEBEN generarse de `ops.registry.REGISTRY`; no DEBE existir otra lista de operaciones en el código.
- **REQ-ARCH-22.** Una prueba DEBE verificar que `REGISTRY` contiene exactamente las operaciones marcadas "sí" en 00-marco §5.7 (salvo `aucove hook <evento>`, `aucove mcp --stdio` y `aucove ci <subcomando>`, que despacha `entry`), que ninguna operación "reservado" está registrada y que toda operación con `human_only=True` tiene `mcp_tool=None`.
- **REQ-ARCH-23.** Solo `cli` PUEDE poner `ctx.human_confirmed=True`, y solo tras las dos fases de `require_human` de 06-permisos §6: antes de credenciales, `E_HUMAN_ONLY_AGENT_ENV` si `CLAUDE_CODE_CHILD_SESSION` no está vacía o `CI`/`GITHUB_ACTIONS` valen `true`, y `E_HUMAN_ONLY_NO_TTY`; después de las lecturas, el reto tecleado (`E_HUMAN_ONLY_NOT_CONFIRMED`). `mcp` y `hooks` DEBEN usar `human_confirmed=False`.
- **REQ-ARCH-24.** La salida de toda operación, por cualquier envoltura, DEBE ser el sobre de resultado de 05-operaciones, generado por `ops`.

### 3.5 Contratos de importación verificables

- **REQ-ARCH-25.** El repositorio DEBE declarar contratos de `import-linter` (tipo `layers` y `forbidden`) que codifiquen la tabla de §3.1, y el CI del framework DEBE ejecutarlos.
- **REQ-ARCH-26.** Una prueba DEBE ejecutar `aucove hook pre-tool-use` en proceso con la caché caliente y comprobar que, al terminar, `sys.modules` no contiene ninguno de estos módulos: `httpx`, `httpx2`, `truststore`, `mcp`, `mcp_types`, `pydantic`, `anyio`, `starlette`, `sse_starlette`, `uvicorn`, `opentelemetry`, `jwt`, `multipart`, `jsonschema`, `yaml`, `argparse`, `aucove.cli`, `aucove.mcp`, `aucove.forge.github`, `aucove.render`.

## 4. Dependencias

| Necesidad | Elección | Rango en la rueda | En ruta caliente | Motivo | Descartado |
|---|---|---|---|---|---|
| CLI | `argparse` (stdlib) | — | no | sin dependencias; el parser se genera del registro; ayuda localizable; la ruta de hooks no lo usa | Typer (arrastra Click y Rich, más arranque); Click (válido, pero no aporta frente a un parser generado) |
| YAML | PyYAML, con `CSafeLoader` si está disponible | `>=6.0.2,<7` | solo en ruta fría | madura; con libyaml es rápida; v0.1 solo necesita lectura segura | ruamel.yaml (ida y vuelta innecesaria en v0.1) |
| JSON Schema | `jsonschema[format-nongpl]` (Draft 2020-12) | `>=4.23,<5` | no | implementación de referencia; `referencing.Registry` local sin red; el extra `format-nongpl` aporta los verificadores de `format` que exige REQ-SCHEMA-6, sin dependencias GPL | fastjsonschema (solo hasta draft-07) |
| Versiones | `packaging` | `>=24` | no (el resultado se cachea) | `SpecifierSet` de PEP 440 | parser propio |
| HTTP | `httpx2` (import `httpx2`) | `>=2.13,<3` | no | timeouts por fase, proxies del entorno, `MockTransport` para pruebas (si `httpx2` no lo trae, su transporte de pruebas equivalente); es el cliente que ya usa el SDK MCP | `httpx` (sin versiones estables desde 2024; el SDK MCP ya usa `httpx2`); requests; `urllib` |
| TLS | `truststore` | `>=0.10,<1` | no | usa el almacén de certificados del sistema operativo (proxies corporativos con inspección TLS) | solo certifi |
| MCP | SDK oficial `mcp` v2 (`from mcp.server import MCPServer`) | `>=2.2,<3` | no | SDK Tier 1; v2 es la línea estable y soporta la revisión 2026-07-28 y las anteriores; stdio es el transporte por defecto | FastMCP 4.x (queda como alternativa si el modo completo lo necesita; REQ-ARCH-16 lo aísla) |
| TOML | `tomllib` (stdlib) | — | sí | catálogos y metadatos de skills | — |
| Git | ejecutable `git` por `subprocess` | — | no (la ruta caliente lee archivos de `.git`) | sin binarios nativos; mismo comportamiento que el usuario | GitPython, pygit2 |
| Sellos RFC 3161 (extra `evidence`) | `rfc3161-client` | `>=1.0,<2` | no | arma la petición (SHA-256, nonce, `certReq`) y verifica respuesta y cadena; OCSP con `cryptography.x509.ocsp` | `openssl ts` por subprocess (Git for Windows lo trae) |
| OpenTimestamps (extra `evidence`) | `opentimestamps` (LGPL-3.0-or-later; trae `python-bitcoinlib`) | `>=0.4.5,<0.5` | no | serialización de pruebas, calendarios y `upgrade` | cliente `ots` por subprocess |
| Zonas horarias en Windows (extra `evidence`) | `tzdata` | `>=2025.2`, solo si `sys_platform == "win32"` | no | `zoneinfo` sin base del sistema | mostrar solo UTC |
| Firma del proyecto (extra `evidence`) | `cryptography` (Apache-2.0 o BSD-3-Clause; ya la trae `rfc3161-client`) | `>=44` | no | Ed25519 y llaves OpenSSH para firmar y verificar SSHSIG en `ci seal` y `evidence verify` (11-trazabilidad §8.10); `evidence sign` firma con `ssh-keygen` | `ssh-keygen -Y` por subprocess |

Extracto normativo de `pyproject.toml`:

```toml
[build-system]
requires = ["hatchling>=1.26"]
build-backend = "hatchling.build"

[project]
name = "aucove"
dynamic = ["version"]
requires-python = ">=3.11"
license = "Apache-2.0 AND MIT-0 AND CC0-1.0"
license-files = ["LICENSE", "NOTICE", "LICENSES/Apache-2.0.txt", "LICENSES/MIT-0.txt", "LICENSES/CC0-1.0.txt"]
dependencies = [
  "PyYAML>=6.0.2,<7", "jsonschema[format-nongpl]>=4.23,<5", "packaging>=24",
  "httpx2>=2.13,<3", "truststore>=0.10,<1", "mcp>=2.2,<3",
]

[project.optional-dependencies]
evidence = ["rfc3161-client>=1.0,<2", "opentimestamps>=0.4.5,<0.5", "cryptography>=44",
            "tzdata>=2025.2; sys_platform == 'win32'"]

[project.scripts]
aucove = "aucove.entry:main"

[tool.hatch.version]
path = "src/aucove/_meta.py"

[tool.hatch.build.targets.wheel]
packages = ["src/aucove"]

[tool.hatch.build.targets.wheel.force-include]
"skills" = "aucove/_data/skills"
"template" = "aucove/_data/template"
"schemas" = "aucove/_data/schemas"
"tools/template_vars.toml" = "aucove/_data/template_vars.toml"

# El sdist lleva el código, sus pruebas y las fuentes que van en la rueda, sin la
# especificación (CC-BY-4.0) ni los archivos de gobierno del repositorio (01 §5.1).
[tool.hatch.build.targets.sdist]
only-include = [
  "src", "tests", "schemas", "skills", "template", "tools",
  "LICENSE", "NOTICE", "LICENSES/Apache-2.0.txt", "LICENSES/MIT-0.txt", "LICENSES/CC0-1.0.txt",
  "REUSE.toml", "README.md", "pyproject.toml", "uv.lock",
]
```

- **REQ-ARCH-27.** Toda dependencia nueva de ejecución DEBE justificarse en esta tabla (revisión de la especificación) y NO DEBE importarse en la ruta caliente.
- **REQ-ARCH-99.** El extra `evidence` NO DEBE importarse en la ruta caliente ni ser requisito de ninguna operación, salvo `ci seal` y `evidence sign` (00-marco D-72). Sin el extra, `evidence verify` verifica el manifiesto, las aperturas, los actores y git, deja en `skip` las pruebas TSA y OTS y la firma, y agrega `W_CONFIG_TOOL_MISSING`; `doctor` lo informa en el check `evidence_tools`. El CI lo instala con `uv tool install "aucove[evidence]==$PIN" --constraints requirements-lock.txt` (09-ci §7.4).
- **REQ-ARCH-28.** La validación con JSON Schema DEBE usar un `referencing.Registry` construido con los esquemas empaquetados en `aucove/_data/schemas`; NO DEBE resolver `$id` ni `$ref` por red (los `$id` usan el dominio `.invalid`).
- **REQ-ARCH-29.** `yaml.safe_load` (o `CSafeLoader`) DEBE ser el único cargador YAML; `validate` DEBE reportar con una pista (`hint`) los valores que YAML 1.1 convierte de forma implícita (por ejemplo, `convention: 0.1` sin comillas se lee como número).

## 5. Distribución e instalación

### 5.1 Canal y formas de instalación

El núcleo se publica **solo en PyPI** como rueda `py3-none-any` más sdist (§2.3). La rueda lleva el paquete, con `py.typed` (PEP 561), y los datos de REQ-ARCH-3. El sdist lleva el código, sus pruebas, `schemas/`, `skills/`, `template/`, `tools/`, los textos de las licencias de la rueda, `REUSE.toml`, `README.md`, `pyproject.toml` y `uv.lock`; no lleva la especificación (CC-BY-4.0) ni los archivos de gobierno del repositorio (00-marco D-54). Cada GitHub Release adjunta además `requirements-lock.txt` (versiones exactas y hashes de las dependencias de ejecución) y `sbom.cdx.json` (§2.3; 10-seguridad §6.2).

La instalación normativa (macOS/Linux, Windows, devcontainer y CI) descarga `https://github.com/<org del framework>/aucove/releases/download/vX.Y.Z/requirements-lock.txt` a un archivo temporal y ejecuta `uv tool install "aucove==X.Y.Z" --constraints <archivo>` (10-seguridad REQ-SEC-45). El setup script de la nube (§5.3) queda exento, porque allí las peticiones de *release assets* de GitHub solo alcanzan los repositorios adjuntos a la sesión; es un riesgo residual (10-seguridad REQ-SEC-45).

| Entorno | Instalación normativa |
|---|---|
| macOS / Linux | `curl -LsSf https://astral.sh/uv/install.sh \| sh` ; `LOCK="$(mktemp)"` ; `curl -fsSL -o "$LOCK" https://github.com/<org del framework>/aucove/releases/download/vX.Y.Z/requirements-lock.txt` ; `UV_COMPILE_BYTECODE=1 uv tool install "aucove==X.Y.Z" --constraints "$LOCK"` ; `uv tool update-shell` |
| Windows (PowerShell 5.1 o 7) | `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` ; `$lock = New-TemporaryFile` ; `Invoke-WebRequest -UseBasicParsing https://github.com/<org del framework>/aucove/releases/download/vX.Y.Z/requirements-lock.txt -OutFile $lock.FullName` ; `$env:UV_COMPILE_BYTECODE="1"; uv tool install "aucove==X.Y.Z" --constraints $lock.FullName` ; `uv tool update-shell` ; reiniciar la terminal y Claude Code |
| Devcontainer | §5.2 (mismo esquema, con `X.Y.Z` = `core_pin`) |
| Alternativa sin uv | `pipx install "aucove==X.Y.Z"` |
| CI del proyecto | descarga el `requirements-lock.txt` de `v<core_pin>` ; `uv tool install "aucove==<core_pin>" --constraints <archivo>` (el workflow lo define 09-ci) |

`uv tool install` crea un entorno aislado y coloca el ejecutable en `~/.local/bin` (Unix) o `%USERPROFILE%\.local\bin` (Windows). En Windows uv copia un lanzador `.exe` (trampolín) por cada script de consola, así que `aucove` es un ejecutable real. Esto importa porque, en forma exec, un hook de Claude Code en Windows exige que `command` resuelva a un `.exe`; los shims `.cmd`/`.bat` no se pueden lanzar sin shell. En S4, Claude Code 2.1.289 sí lanzó un `.cmd` del `PATH` en forma exec, pero eso no está documentado, así que el requisito se mantiene (00-marco D-52).

- **REQ-ARCH-30.** Toda instrucción de instalación publicada (docs, devcontainer, setup script, CI) DEBE fijar la versión exacta (`aucove==X.Y.Z`).
- **REQ-ARCH-31.** Los hooks generados y el registro del servidor MCP DEBEN invocar al núcleo como `command: "aucove"` en forma exec (con `args`), resuelto por `PATH`. Los hooks de Codex son la excepción: usan la cadena fija `aucove hook <evento> --door codex`, porque Codex no admite la forma exec (07-hooks §13; 00-marco D-58). NO DEBEN usar `uvx`, `npx`, `pipx run`, `uv run` ni rutas absolutas del usuario. (El contenido exacto de esas entradas lo define 07-hooks.)
- **REQ-ARCH-32.** La documentación DEBE indicar `UV_COMPILE_BYTECODE=1` (o `pipx install` con compilación de bytecode) para que la primera invocación de un hook no pague la compilación a `.pyc`.
- **REQ-ARCH-33.** `aucove doctor` DEBE verificar que `aucove` se encuentra en las carpetas absolutas del `PATH`, sin el directorio actual (en Windows, `shutil.which` de Python 3.11 lo antepone siempre, así que no sirve tal cual) y con `PATHEXT` en Windows, y, en Windows, que termina en `.exe`; si no, DEBE mostrar el comando de instalación y `uv tool update-shell`. Además, DEBE dar `warn` si el `PATH` tiene entradas que dependen del directorio actual: relativas (`.`, `bin`; en Windows también `C:bin` y `\bin`) o, fuera de Windows, vacías. Claude Code no busca el `command` de un hook en el directorio actual, pero resuelve esas entradas contra el directorio del hook, que está dentro del proyecto, así que un `aucove` que esté ahí se ejecutaría en lugar del instalado (S4, 00-marco D-52; 10-seguridad TH-24).
- **REQ-ARCH-34.** Un binario autónomo (PyInstaller o similar) NO DEBE ofrecerse en v0.1: su arranque en modo de un solo archivo (descomprime en cada ejecución) es incompatible con el presupuesto de §7. Queda reservado (Q-ARCH-7).
- **REQ-ARCH-35.** El núcleo NO DEBE distribuirse dentro de un plugin ni en un directorio `bin/`; el plugin de catálogo es fase 2 (D-05).
- **REQ-ARCH-97.** El núcleo DEBE invocar `git`, `gh` y `latexmk` con la ruta absoluta de una búsqueda en `PATH` que excluya el directorio actual y todo directorio del repositorio; en Windows DEBE además definir `NoDefaultCurrentDirectoryInExePath=1` en el entorno de los hijos.

### 5.2 Devcontainer

El archivo completo lo define 02-convencion (clase `lead`). Requisitos de instalación:

- **REQ-ARCH-36.** El devcontainer DEBE instalar uv con el instalador de versión fijada (`https://astral.sh/uv/<versión>/install.sh`) y luego el núcleo con la instalación normativa de §5.1 (`uv tool install "aucove==$PIN" --constraints <archivo>`), donde `$PIN` se lee de `project.yaml: core_pin` (§6.2).
- **REQ-ARCH-37.** El devcontainer DEBE dejar el directorio de ejecutables de uv en `PATH` para el usuario remoto (por ejemplo con `remoteEnv`), para que Claude Code encuentre `aucove`.

Ilustrativo (02-convencion fija los archivos reales; el contenedor es Linux, así que aquí Bash sí está garantizado):

```bash
# postCreateCommand: bash .devcontainer/install-core.sh
set -euo pipefail
curl -LsSf https://astral.sh/uv/0.12.20/install.sh | sh
PIN="$(sed -nE 's/^core_pin:[[:space:]]*"([0-9]+\.[0-9]+\.[0-9]+)"[[:space:]]*(#.*)?$/\1/p' project.yaml)"
test -n "$PIN" || { echo "core_pin no encontrado en project.yaml" >&2; exit 1; }
LOCK="$(mktemp)"
curl -fsSL -o "$LOCK" "https://github.com/<org del framework>/aucove/releases/download/v${PIN}/requirements-lock.txt"
UV_COMPILE_BYTECODE=1 "$HOME/.local/bin/uv" tool install "aucove==${PIN}" --constraints "$LOCK"
```

### 5.3 Sesiones de Claude Code en la nube (web)

Hechos verificados (cloud-environments): el setup script es un script Bash que corre **como root en Ubuntu 24.04**, antes de que arranque Claude Code, solo en sesiones nuevas; su resultado se cachea como instantánea del sistema de archivos (si termina en unos 5 minutos) y se reconstruye al cambiar el script o a los ~7 días. Si sale con código distinto de 0, la sesión no arranca. El nivel de red por defecto, **Trusted**, incluye `pypi.org` y `files.pythonhosted.org`. La imagen trae Python 3 con pip y **uv**, y cambia sin aviso: en S1 (00-marco D-41) traía Python 3.11.15 y uv 0.8.17, y en S2 (D-59), Python 3.13.16 y uv 0.11.32, con `uv` en `/root/.local/bin`, que está en el `PATH` del setup script. El setup script corre en `/home/user`, con el repositorio ya clonado en `/home/user/<repo>`; aun así no instala desde el clon, porque el entorno es de la cuenta y no de un repositorio (D-59). Los `.claude/settings.json` (hooks), `.claude/skills/` y `.mcp.json` del repositorio sí se cargan en una sesión con un solo repositorio; los plugins declarados por el repo, no. Las peticiones de *release assets* de GitHub solo alcanzan los repositorios adjuntos a la sesión, así que uv no puede descargar su Python gestionado desde GitHub.

Decisión: la instalación en la nube se hace por **setup script**, no por un hook SessionStart. Un hook de instalación tendría que filtrar por `CLAUDE_CODE_REMOTE` con una shell, y en Windows local caería en PowerShell; en forma exec no hay forma de filtrar, y un `uv tool install` en cada inicio local modificaría la instalación global del usuario.

```bash
#!/bin/bash
# Setup script del entorno cloud. PIN = project.yaml: core_pin del proyecto.
PIN="0.1.3"
export UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 UV_TOOL_DIR=/opt/aucove/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin
uv tool install --python "$(command -v python3)" "aucove==${PIN}" \
  || echo "aucove: instalación fallida; el CI sigue haciendo cumplir las reglas" >&2
chmod -R a+rX /opt/aucove 2>/dev/null || true
exit 0
```

Una versión preliminar no se instala desde un índice: ver 12-mvp REQ-MVP-46.

- **REQ-ARCH-38.** La documentación DEBE publicar este setup script, y DEBE indicar que en Team/Enterprise un Owner puede crearlo una vez como entorno compartido de la organización.
- **REQ-ARCH-39.** El setup script DEBE terminar siempre con código 0 y DEBE deshabilitar las descargas de Python de uv (`UV_PYTHON_DOWNLOADS=never`).
- **REQ-ARCH-40.** El `AGENTS.md` de la plantilla (02-convencion) DEBE indicar que, si `aucove --version` falla o `doctor` informa un desajuste, el agente se detenga y pida a la persona instalar el núcleo con el comando de esta guía; el agente NO DEBE instalarlo.
- **REQ-ARCH-98.** La guía de la puerta web DEBE indicar que cada sesión se abre con un solo repositorio: con varios no se cargan `.claude/settings.json`, `.claude/skills/` ni `.mcp.json`, y no corre ningún hook del repositorio (07-hooks §12).
- **REQ-ARCH-41.** Cómo se autentica el núcleo ante GitHub dentro de una sesión en la nube (proxy de GitHub) NO se define aquí: es de 08-forja.

## 6. Versionado y compatibilidad

### 6.1 SemVer del núcleo

- **REQ-ARCH-42.** El núcleo DEBE seguir SemVer 2.0.0. Mientras la mayor sea `0`, un cambio de menor (`0.1` → `0.2`) PUEDE romper compatibilidad y un cambio de parche (`0.1.3` → `0.1.4`) NO DEBE: ni cambiar de forma incompatible el `schema` de la salida (05-operaciones), ni el contrato de hooks (07-hooks), ni la semántica de la convención, ni los archivos generados salvo por correcciones.
- **REQ-ARCH-43.** Cada versión del núcleo DEBE declarar en `_meta.SUPPORTED_CONVENTIONS` las versiones de convención que acepta. La serie `0.1.x` acepta solo `"0.1"`.

### 6.2 Claves de `project.yaml`

El esquema es de 03-esquemas; este capítulo fija la semántica (00-marco §7):

```yaml
convention: "0.1"              # versión de la convención (00-marco §7)
core: ">=0.1.3,<0.2"           # rango PEP 440 de versiones del núcleo compatibles (propuesto, §12)
core_pin: "0.1.3"              # versión exacta que instalan CI, devcontainer y nube (propuesto, §12)
language: "es"                 # 00-marco §5.8
```

- **REQ-ARCH-44.** `init` DEBE escribir `core` como `>=X.Y.Z,<X.(Y+1)` cuando la mayor es 0, o `>=X.Y.Z,<(X+1)` desde 1.0, y `core_pin` como la versión instalada que ejecuta `init`. Si la instalada es una pre-release (00-marco D-66), la cota inferior es esa misma versión: `>=0.1.0rc1,<0.2`.
- **REQ-ARCH-45.** `core_pin` DEBE satisfacer `core`, y DEBE aparecer en una sola línea de nivel superior con el formato `^core_pin: "[0-9]+\.[0-9]+\.[0-9]+((a|b|rc)[0-9]+)?"( *#.*)?$`, para que CI y scripts lo extraigan sin YAML; `validate` DEBE comprobar ese formato de línea además del esquema.
- **REQ-ARCH-46.** La compatibilidad DEBE evaluarse con el `project.yaml` de la rama base (`origin/<forge.base_branch>`), igual que `roles.yaml` (D-08), no con el del árbol de trabajo.

### 6.3 Algoritmo

```python
# domain/compat.py (puro); packaging se importa en config y el resultado se cachea (§7.4)
def check(installed: Version, convention: str, core: SpecifierSet, pin: Version | None) -> Compat:
    if convention not in SUPPORTED_CONVENTIONS:
        return Compat(level="error", code="E_CONFIG_CONVENTION_UNSUPPORTED")
    if installed not in core:
        return Compat(level="error", code="E_CONFIG_CORE_MISMATCH")
    if pin is not None and installed != pin:
        return Compat(level="warning", code=None)   # dentro del rango: solo aviso
    return Compat(level="ok", code=None)
```

La semántica de referencia de `domain/compat.py` es la de `packaging` en la versión que fija `uv.lock` (26.3). Esa versión evalúa `<V` y `>V` por rangos y excluye solo la familia de V: por ejemplo, `<1.post0` admite `1a0`, y `>1.dev0` admite `1+abc`. Las versiones 26.0 y anteriores excluían toda la release base. Las pruebas diferenciales de `tests/unit/test_compat.py` comparan las dos implementaciones con la versión del lock.

### 6.4 Comportamiento ante desajuste

| Comando | `level == "error"` | `level == "warning"` |
|---|---|---|
| `doctor` | nunca aborta: reporta el chequeo fallido con el comando exacto de §5.1 para `core_pin`, `uv tool install "aucove==<core_pin>" --constraints requirements-lock.txt`, con el `requirements-lock.txt` de esa Release (10-seguridad REQ-SEC-45) | lo reporta |
| Solo lectura (`status`, `context`, `check-path` por CLI, `review list`, `review show`, `metrics`) | corre y agrega una advertencia (formato: 05-operaciones) | advertencia |
| `init` sobre un repo recién creado desde la plantilla, o con `--repo` en una carpeta vacía (00-marco D-73) | solo exige que la convención esté soportada; reescribe `core` y `core_pin` (REQ-ARCH-44) | se ejecuta |
| `validate` | falla con el código de §6.3 (en CI la versión es siempre `core_pin`) | advertencia |
| Operaciones con efectos (`task propose`, `task accept`, `task cancel`, `claim`, `release`, `save`, `update`, `switch`, `record-assessment`, `submit`, `expire-claims`, `decision propose`, `direction propose`, `review approve`, `review request-changes`), `sync-roles`, `init --regenerate` | NO DEBEN ejecutarse: error `E_CONFIG_CORE_MISMATCH` o `E_CONFIG_CONVENTION_UNSUPPORTED`, `retryable: false`, con el comando de instalación en `hint` | se ejecutan (`init --regenerate` exige además versión == `core_pin`, §10.7) |
| `aucove hook <evento>` | **cerrada**: todo hook que puede bloquear DEBE bloquear la acción sobre rutas del proyecto con un mensaje localizado que incluye el comando de instalación; los hooks que no bloquean (inicio de sesión) DEBEN emitir el aviso como contexto. La forma concreta la define 07-hooks | sin efecto en decisiones; `session-start` incluye una línea de aviso |
| `mcp --stdio` | arranca; cada herramienta responde según la fila de su operación | advertencia en cada respuesta |

- **REQ-ARCH-47.** Ante un error de compatibilidad, la ruta de hooks NO DEBE quedar abierta (tabla anterior).
- **REQ-ARCH-48.** Si `aucove hook` no encuentra un `project.yaml` válido en la raíz del repositorio git que contiene `cwd`, DEBE bloquear igual que ante un error de compatibilidad; solo si `cwd` no está dentro de ningún repositorio git PUEDE terminar con 0 sin salida. En v0.1 los hooks solo los declara el `.claude/settings.json` del proyecto, así que invocarlos implica estar en un proyecto.
- **REQ-ARCH-49.** `docs/` DEBE publicar una matriz de compatibilidad por versión del núcleo: convenciones soportadas, Python mínimo, versión mínima de Claude Code (2.1.251 en v0.1, requerida para `PostModelSwitch`, 07-hooks) y estado de Codex, Antigravity CLI, Gemini CLI, OpenCode y goose (mejor esfuerzo).

## 7. Rendimiento

### 7.1 Presupuestos

| Ruta | Métrica | Presupuesto | Condiciones de medición |
|---|---|---|---|
| `aucove hook pre-tool-use` sobre `Edit\|Write\|NotebookEdit`, caché caliente | p95 de tiempo de pared, del lanzamiento del proceso a su salida | **< 150 ms** en Linux y macOS; **< 300 ms** en Windows; 0 subprocesos | sin red (`AUCOVE_OFFLINE=1`), bytecode compilado, 200 invocaciones; umbrales por runner en REQ-ARCH-55 |
| `aucove hook pre-tool-use` sobre `Bash\|PowerShell` | p95 | < 400 ms; como máximo 1 subproceso `git` de lectura | sin red, caché caliente |
| `aucove hook post-tool-use` y `post-tool-use-failure` | p95 | < 200 ms; < 450 ms en Windows; ≤ 3 subprocesos | sin red, caché caliente |
| `aucove hook stop`, `aucove hook config-change` | p95 | < 150 ms; < 300 ms en Windows | sin red, caché caliente |
| `aucove hook session-end` | p95 | < 300 ms; < 600 ms en Windows | sin red, caché caliente |
| `aucove hook <evento>` fuera de un proyecto del framework (REQ-ARCH-48) | p95 | < 60 ms; < 120 ms en Windows | sin red |
| `aucove hook session-start` | p95 | < 1000 ms | sin red, caché caliente; el refresco, si existe, va en segundo plano |
| `aucove hook <evento>`, caché fría (reconstruye `policy.json`) | p95 | < 1000 ms | sin red; `git` local |
| `aucove context` / `get_context` | longitud | **≤ `project.yaml: context.max_chars`** unidades UTF-16 (defecto 8000) | siempre |
| `aucove context` | p95 | < 500 ms | caché caliente, sin red |
| `aucove check-path` (CLI) | p95 | < 400 ms | sin red |
| `aucove mcp --stdio` | hasta responder la primera petición (`initialize` en 2025-11-25, `server/discover` en 2026-07-28) | DEBERÍA < 2000 ms | Windows |

El valor por defecto del tope de `context` (8000) se deriva del límite verificado de Claude Code: `additionalContext`, `systemMessage` y el stdout de un hook se cortan a 10 000 caracteres; lo que excede se guarda en un archivo y Claude solo recibe la ruta y una vista previa de 2000 caracteres, sin que se le pida leerlo. El 20 % de margen cubre el envoltorio que agregue 07-hooks.

- **REQ-ARCH-50.** `ops.context` DEBE garantizar `len(texto.encode("utf-16-le")) // 2 <= context.max_chars`. Si el contenido excede el tope, DEBE descartar bloques completos en el orden de prioridad que fija 07-hooks y terminar con una línea localizada que remita a `aucove status`; NO DEBE cortar a mitad de un bloque. DEBERÍA apuntar a ≤ 4000 en un proyecto típico.
- **REQ-ARCH-51.** `pre-tool-use` de edición NO DEBE lanzar subprocesos; el de shell PUEDE lanzar uno (07 §4.3); ninguno abre sockets.
- **REQ-ARCH-52.** La ruta caliente DEBE obtener la rama actual leyendo `<gitdir>/HEAD` y el oid de `refs/remotes/origin/<base>` leyendo el ref suelto o `packed-refs` del directorio común (resolviendo `.git` como archivo `gitdir:` y el archivo `commondir` en worktrees). Si no encuentra el ref por esa vía (por ejemplo, con reftable), DEBE pasar a la ruta fría.
- **REQ-ARCH-53.** Toda salida de máquina (hooks, MCP stdio, salida JSON de la CLI) DEBE escribirse como UTF-8 sin BOM sobre `sys.stdout.buffer`, sin depender de la página de códigos de la consola; los hooks DEBERÍAN serializar JSON con `ensure_ascii=True`.
- **REQ-ARCH-54.** Si 07-hooks define un refresco en segundo plano, el proceso hijo DEBE crearse desacoplado (Windows: `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`; POSIX: `start_new_session=True`), con stdin, stdout y stderr en `DEVNULL` (si hereda los pipes, Claude Code espera a que se cierren), con un tope de 60 s y escribiendo solo en `.aucove/`.

### 7.2 Medición

- **REQ-ARCH-55.** `tests/perf/` DEBE incluir una prueba que lance 200 veces `aucove hook pre-tool-use` como subproceso con un JSON de entrada de `tests/fixtures/`, descarte las 5 primeras, calcule el p95 y lo compare con el presupuesto de §7.1: estricto en `ubuntu-latest` y en el portátil Windows 11 de referencia; en `windows-latest` y `macos-latest` falla por encima de 1,5 veces el presupuesto. DEBE correr en `ubuntu-latest`, `windows-latest` y `macos-latest` en cada PR que toque `hooks`, `domain`, `localstore`, `vcs`, `i18n`, `obs` o `entry`.
- **REQ-ARCH-56.** `aucove doctor` DEBERÍA medir 10 invocaciones locales de la ruta caliente (con una entrada sintética que no registra eventos) y avisar si el p95 supera el presupuesto, para detectar máquinas lentas en el piloto.

### 7.3 Caché en `.aucove/`

```
.aucove/                    # en el checkout principal; lo comparten todos los worktrees (REQ-ARCH-61)
  .gitignore                  # contiene "*": nada se versiona aunque falte la regla en .gitignore
  cache/
    policy.json               # permisos compilados desde roles.yaml y project.yaml de la base, sus sha256,
                              #   resultado de compatibilidad y mapa casefold(ruta) -> ruta de git ls-files
    identity.json             # identidad (contenido y validez: 08-forja)
    capabilities.json         # perfil enforced/advisory (08-forja)
    forge-snapshot.json       # Issues, reclamos, PRs y revisiones (lo llena ops; lo lee context/status)
    local-rules.json          # manifiesto de las reglas de settings.local.json que son del núcleo (07-hooks)
    tasks/<n>.json            # claimant, state, sections efectivas, local_branch, fetched_at y, desde
                              #   status o context, la derivación y su huella (00-marco D-61)
    http/                     # respuestas GET con ETag para peticiones condicionales (08-forja)
  state/
    heartbeat.json            # último SessionStart (07-hooks)
    notices.json              # avisos pendientes para el siguiente Stop o SessionStart (07-hooks)
    sessions/<session_id>.json            # registro de sesión (11-trazabilidad)
    worktrees/<id>/pending.json           # (07-hooks)
    worktrees/<id>/session-state.json     # (07-hooks)
  evidence/<kind>-<commit>-a<n>/  # copia de los registros ci:evidence de los PR propios (05-operaciones REQ-OPS-66): no es caché
  work/                       # trabajo recuperable: no es caché (REQ-ARCH-59)
    quarantine/<id>/<YYYYMMDDTHHMMSSZ>-<tool_use_id>/
    dropped/<id>/<YYYYMMDDTHHMMSSZ>-task-<n>.patch
    snapshots/<id>/<tool_use_id>/
  locks/<clave>.lock          # candados locales (05-operaciones §6.4)
  context.md                  # resumen en Markdown que lee cualquier agente al empezar (05-operaciones REQ-OPS-67; 00-marco D-58)
  log/
    events.jsonl  events.1.jsonl  events.2.jsonl  events.3.jsonl   # único registro local (§11)
  review/                     # entradas de la revisión con modelo en CI (09-ci)
  tmp/
```

`<id>` = primeros 12 hex del SHA-256 de la ruta real del worktree. No existe `context.json`. En `policy.json`, `key.base_oid` va acompañado del sha256 de los `roles.yaml` y `project.yaml` leídos (10-seguridad REQ-SEC-82), y `data` incluye el mapa `casefold(ruta) → ruta` de `git ls-files`.

Sobre común de toda entrada de caché:

```json
{"cache_schema": 1, "core": "0.1.3", "created_at": "2026-09-29T14:03:12Z",
 "key": {"base_oid": "4f2c...e91a", "base_branch": "main"}, "data": {}}
```

| Entrada | Clave de validez | Frescura | Si falta, está corrupta o es inválida |
|---|---|---|---|
| todas | `cache_schema` y `core` iguales a los del proceso | — | inválida |
| `policy.json` | `base_oid` == oid actual de `refs/remotes/origin/<base>` | sin TTL | se reconstruye en la ruta fría (`git show <oid>:roles.yaml` y `:project.yaml`); si falla, la ruta de hooks se cierra |
| `forge-snapshot.json` | `repo` (dueño/nombre) igual | fresca 10 min; utilizable hasta 7 días con advertencia `as_of` | las operaciones que leen la forja la refrescan si hay red; sin red, las `degradada` responden desde ella con `W_NET_OFFLINE` y las `requiere` fallan (§8) |
| `identity.json`, `capabilities.json` | las fija 08-forja | revalidación en línea como máximo cada 24 h | las fija 08-forja |

- **REQ-ARCH-57.** Toda escritura en `.aucove/` DEBE ser atómica: archivo temporal en el mismo directorio y `os.replace`. En Windows, si `os.replace` falla con `PermissionError`, DEBE reintentar hasta 3 veces con 10 ms de espera y después abandonar sin error, porque la caché es descartable.
- **REQ-ARCH-58.** Una lectura de caché que falle al decodificar o no cumpla el sobre DEBE tratarse como ausencia, nunca como error de la operación.
- **REQ-ARCH-59.** Borrar `.aucove/` DEBE ser seguro salvo `work/` y `evidence/`, que pueden contener trabajo recuperable o evidencia; `doctor` DEBE avisar si `work/` no está vacío. Ningún proceso del núcleo DEBE borrar `evidence/`; `doctor` informa cuántos registros guarda.
- **REQ-ARCH-60.** `.aucove/` NO DEBE contener secretos (tokens, claves ni contraseñas); el almacenamiento de credenciales lo define 08-forja.
- **REQ-ARCH-61.** `.aucove/` DEBE ubicarse en el checkout principal (padre de `commondir`) y lo comparten todos los worktrees; las entradas que dependen del worktree van bajo `state/worktrees/<id>/` y `work/…/<id>/`. La clave de `policy.json` no depende del worktree.

## 8. Comportamiento sin red

### 8.1 Principios

- **REQ-ARCH-62.** Ninguna llamada de red del núcleo DEBE hacerse sin timeout: conexión 5 s y lectura 15 s, salvo las de la forja, cuyos timeouts fija 08-forja REQ-FORGE-23. En CLI y MCP, la red de una operación DEBE sumar como máximo 120 s; en CI rigen las esperas de 08-forja §7.3 (00-marco D-60). Los reintentos y los límites de la API los define 08-forja.
- **REQ-ARCH-63.** Con `AUCOVE_OFFLINE=1`, toda llamada de red DEBE fallar de inmediato con `E_NET_OFFLINE`, sin abrir sockets.
- **REQ-ARCH-64.** Todo subproceso `git` DEBE correr con `GIT_TERMINAL_PROMPT=0` (para que nunca espere credenciales interactivas) y con timeout; las lecturas DEBEN usar además `GIT_OPTIONAL_LOCKS=0`.
- **REQ-ARCH-65.** El cliente HTTP DEBE respetar `HTTPS_PROXY`, `HTTP_PROXY` y `NO_PROXY`, y verificar TLS con `SSL_CERT_FILE` si está definido o, si no, con el almacén del sistema vía `truststore`. Los errores DEBEN clasificarse así: resolución DNS, conexión rechazada o timeout de conexión → `E_NET_OFFLINE` (`retryable: true`); timeout de lectura → `E_NET_TIMEOUT` (`retryable: true`); verificación de certificado → `E_NET_TLS` (`retryable: false`, con `hint` sobre el proxy corporativo y `SSL_CERT_FILE`).

### 8.2 Comportamiento de cada operación sin red

El comportamiento sin red de cada operación lo fija la columna «Sin red» de 05-operaciones §9.1, con tres valores: `local` (funciona igual), `degradada` (funciona con caché o solo en local y avisa con `W_NET_OFFLINE`) y `requiere` (falla con `E_NET_OFFLINE` antes de escribir). `OpSpec` usa ese campo `offline` de 05 (§3.4); este capítulo no mantiene otra clasificación. No hay cola en v0.1: sin red no se reclama, para no duplicar trabajo.

`sync-roles` lee `roles.yaml` del árbol de trabajo (05 §9.4).

- **REQ-ARCH-66.** Sin red, una persona DEBE poder seguir editando las partes que la caché de `policy.json` le permite; la ruta de hooks nunca depende de la red.
- **REQ-ARCH-67.** Una operación con `Sin red: requiere` NO DEBE dejar efectos locales cuando falla por red antes de su primer efecto remoto; las `degradada` hacen su parte local y avisan con `W_NET_OFFLINE`.
- **REQ-ARCH-68.** `mcp --stdio` DEBE arrancar sin red; cada herramienta se comporta según el valor `offline` de su operación.

## 9. Internacionalización

### 9.1 Qué se traduce

| Nunca se traduce (inglés ASCII) | Se localiza (`es`/`en`) |
|---|---|
| subcomandos, opciones y argumentos de la CLI; nombres de herramientas MCP; claves YAML y JSON; valores enumerados (`enforced`, estados, roles); códigos de error; etiquetas; ramas; rutas; trailers; nombres de eventos del registro local; `data` del sobre | `error.message`, `error.hint` y advertencias; ayuda de la CLI; mensajes de hooks para el agente; contexto de `context`; cuerpo y descripción de skills; archivos localizables de la plantilla; documentación |

### 9.2 Resolución del idioma

- **REQ-ARCH-69.** El idioma de los mensajes en tiempo de ejecución DEBE resolverse en este orden: `AUCOVE_LANG` (si vale `es` o `en`); `project.yaml: language` de la rama base, si hay proyecto; `en`. NO DEBE usar el locale del sistema.
- **REQ-ARCH-70.** El idioma de los archivos generados y de la plantilla DEBE ser siempre `project.yaml: language`, nunca `AUCOVE_LANG`.
- **REQ-ARCH-71.** `init` DEBE preguntar primero el idioma con un texto bilingüe fijo.

### 9.3 Catálogos

Ubicación: `src/aucove/i18n/catalogs/es.toml` y `en.toml`. Las claves son rutas ASCII con puntos; los marcadores usan la sintaxis de `str.format` con nombres simples. Ejemplo (el texto definitivo lo fija el capítulo dueño de cada mensaje):

```toml
# es.toml
[compat]
core_mismatch = "El núcleo instalado ({installed}) no cumple `core: {required}` de `project.yaml`. Instala la versión fijada: `uv tool install \"aucove=={pin}\" --constraints requirements-lock.txt`."

[net]
stale.one = "Datos de la forja de hace {n} hora (sin conexión)."
stale.other = "Datos de la forja de hace {n} horas (sin conexión)."
```

```toml
# en.toml
[compat]
core_mismatch = "The installed core ({installed}) does not satisfy `core: {required}` in `project.yaml`. Install the pinned version: `uv tool install \"aucove=={pin}\" --constraints requirements-lock.txt`."

[net]
stale.one = "Forge data from {n} hour ago (offline)."
stale.other = "Forge data from {n} hours ago (offline)."
```

`t(key, **kw)` busca en el idioma resuelto, luego en `en`, y como último recurso devuelve la clave. `t_plural(key, n, **kw)` elige `.one` si `n == 1` y `.other` en otro caso (regla válida para `es` y `en`).

### 9.4 Reglas verificables

- **REQ-ARCH-72.** `es.toml` y `en.toml` DEBEN tener exactamente el mismo conjunto de claves y, para cada clave, el mismo conjunto de marcadores `{...}`.
- **REQ-ARCH-73.** Para cada clave, la lista de fragmentos entre acentos graves (`` `...` ``) DEBE ser idéntica en ambos catálogos: todo fragmento en código es un identificador y no se traduce.
- **REQ-ARCH-74.** Los marcadores NO DEBEN usar acceso a atributos, índices ni especificadores de formato.
- **REQ-ARCH-75.** La documentación DEBE tener `docs/es/` como fuente; cada archivo de `docs/en/` DEBE declarar en su front matter `source-sha256` del archivo `es` que traduce, y el CI del framework DEBE avisar en los PR y fallar en los releases si no coincide.

### 9.5 Nombres ASCII

`domain.text.ascii_slug(s: str, max_len: int = 48) -> str`: normalización NFKD; quitar marcas combinantes (categoría `Mn`); mapear `ß→ss`, `æ→ae`, `œ→oe`, `ø→o`, `đ→d`, `ł→l`; pasar a minúsculas; reemplazar cada tramo fuera de `[a-z0-9]` por `-`; quitar guiones en los extremos; cortar en `max_len` sin dejar un guion final. Si el resultado queda vacío, devolver `x`.

| Entrada | Salida |
|---|---|
| `Revisión de métodos` | `revision-de-metodos` |
| `Año 2026: ¿qué cambió?` | `ano-2026-que-cambio` |
| `Straße — Œuvre` | `strasse-oeuvre` |

- **REQ-ARCH-76.** Todo nombre de archivo o identificador derivado de texto libre DEBE producirse con `ascii_slug`, y la función DEBE tener pruebas con, al menos, los casos de la tabla. Dónde se usa (por ejemplo, nombres de archivo de decisión) lo definen 02 y 03.

## 10. Generación desde una sola fuente

### 10.1 Verificación de rutas de descubrimiento

| Herramienta | Lee `.claude/skills/` | Lee `.agents/skills/` | Fuente |
|---|---|---|---|
| Claude Code | sí (proyecto, anidados, personal, empresa, plugin) | **no** (no figura en la tabla de ubicaciones) | code.claude.com/docs/en/skills |
| Codex | no | sí, desde `cwd` hasta la raíz del repo | developers.openai.com/codex/skills |
| OpenCode | sí | sí | opencode.ai/docs/skills |
| Gemini CLI | no | sí (alias de `.gemini/skills/`) | https://geminicli.com/docs/cli/skills/ |
| VS Code (GitHub Copilot) | sí | sí (además de `.github/skills/`) | https://code.visualstudio.com/docs/copilot/customization/agent-skills |

Consecuencia: generar ambos destinos no duplica skills en Claude Code. Sí los duplica en OpenCode, que lee las dos rutas con el mismo `name` (Q-ARCH-1). VS Code también lee las dos; qué hace con un mismo nombre en una misma raíz no está documentado.

### 10.2 Formato de la fuente

```
skills/<key>/skill.toml     # <key> = start, status, submit, reviews, decision, direction (clave interna; 00-marco §5.8)
skills/<key>/body.es.md
skills/<key>/body.en.md
```

```toml
# skills/start/skill.toml
key = "start"
cli = ["claim", "task propose"]           # comandos que orquesta; se validan contra ops.registry

[name]                                    # 00-marco §5.8
es = "empezar"
en = "start"

[description]                             # <= 1024 caracteres (Agent Skills)
es = "Empieza a trabajar en una tarea: la reclama, o la propone si no existe, y prepara la rama `task/<n>`."
en = "Start working on a task: claim it, or propose it if it does not exist, and prepare the `task/<n>` branch."

[claude]                                  # campos exclusivos de Claude Code; solo van a .claude/skills
disable-model-invocation = true
argument-hint = { es = "[número de tarea o descripción]", en = "[task number or description]" }
```

```toml
# skills/status/skill.toml (extracto)
key = "status"                            # la clave interna sigue siendo `status`

[name]                                    # 00-marco §5.8
es = "estado"
en = "project-status"                     # `/status` es un comando integrado de Claude Code (REQ-ARCH-78)
```

Marcadores permitidos en los cuerpos (y, solo `var:`, en los archivos de texto de `template/`), con la forma exacta `\{\{(tool|cli|human|skill|var):([A-Za-z0-9_ -]+)\}\}`. No llevan el nombre del proyecto, para que reemplazarlo (D-03) no obligue a editar las fuentes:

| Marcador | `.claude/skills` | `.agents/skills` |
|---|---|---|
| `{{tool:X}}` | `` `mcp__aucove__X` `` | `` `X` `` (herramienta del servidor MCP `aucove`) |
| `{{cli:C}}` | `` `aucove C` `` | igual |
| `{{human:C}}` | `` `aucove C` `` + texto localizado "solo en tu terminal; el agente no puede ejecutarla" | igual |
| `{{skill:K}}` | `` `/<name[lang] de K>` `` | `` `<name[lang] de K>` `` |
| `{{var:V}}` | valor de `project.yaml` (`language`, `core_pin`, `convention`) o, en los archivos de `template/`, una variable de render de la plantilla con valor por defecto (p. ej. `framework_repo`, §2.3) | igual |

### 10.3 Salida por destino

```markdown
---
name: "empezar"
description: "Empieza a trabajar en una tarea: la reclama, o la propone si no existe, y prepara la rama `task/<n>`."
license: "MIT-0"
disable-model-invocation: true
argument-hint: "[número de tarea o descripción]"
metadata:
  aucove-generated: "true"
  aucove-source: "start"
---
<cuerpo de body.es.md con marcadores resueltos para Claude Code>
```

El archivo de `.agents/skills/empezar/SKILL.md` es igual, salvo que su frontmatter solo lleva los campos del estándar Agent Skills (`name`, `description`, `license`, `metadata`) y su cuerpo usa las resoluciones de la columna `.agents/skills`. En los dos destinos, `license: "MIT-0"` viaja con el skill si alguien lo copia a otro sitio; T6 verifica que Claude Code lo acepte [supuesto].

`.mcp.json`, `.gemini/settings.json` y `.codex/config.toml` se generan desde la misma fuente (la definición del servidor `aucove`, cuyo contenido fija 07-hooks). En `.gemini/settings.json` y `.codex/config.toml`, el servidor `aucove` lleva `env` con `AUCOVE_AI_TOOL` = `gemini-cli` o `codex`, respectivamente (11-trazabilidad §2.4). Desde H4, también se genera `.codex/hooks.json` con las definiciones de 07-hooks §13 (00-marco D-58); los hooks de Gemini CLI siguen sin generarse. Codex solo admite el comando como cadena de shell, que es la excepción a REQ-ARCH-31.

- **REQ-ARCH-77.** El frontmatter DEBE empezar en la primera línea del archivo (Claude Code solo lo lee así), con las claves en el orden del ejemplo y cada valor de texto escrito como literal de cadena JSON (`json.dumps(v, ensure_ascii=False)`), que YAML acepta como cadena entre comillas dobles.
- **REQ-ARCH-78.** `name` DEBE cumplir `^[a-z0-9]+(-[a-z0-9]+)*$`, tener 64 caracteres como máximo y coincidir con el directorio; los nombres DEBEN ser únicos por idioma. `name[lang]` NO DEBE coincidir con un comando integrado ni un skill incluido de Claude Code (lista de https://code.claude.com/docs/en/commands, verificada por T10 en cada versión).
- **REQ-ARCH-79.** `description` DEBE tener 1024 caracteres como máximo y, en Claude Code, `description` más `when_to_use` DEBEN sumar 1536 como máximo; el cuerpo renderizado DEBE tener 500 líneas como máximo.
- **REQ-ARCH-80.** Un marcador `tool:` DEBE nombrar una herramienta de `ops.registry`; un marcador `cli:` NO DEBE nombrar una operación H (para eso existe `human:`); cualquier marcador desconocido o sin resolver DEBE hacer fallar el render.
- **REQ-ARCH-81.** Los skills cuya operación tiene efectos DEBEN llevar `disable-model-invocation = true`; que sus herramientas MCP no estén en `permissions.allow` lo exige 07-hooks/06-permisos.
- **REQ-ARCH-82.** El render DEBE ser determinista: UTF-8 sin BOM, LF, un salto de línea final, sin marcas de tiempo ni versión del núcleo en el contenido.

### 10.4 Algoritmo

```python
def render_skills(lang: str, project_vars: dict[str, str]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for src in sorted(DATA / "skills").iterdir():           # aucove/_data/skills
        meta = tomllib.loads((src / "skill.toml").read_text("utf-8"))
        validate_meta(meta)                                   # REQ-ARCH-78..81
        body = (src / f"body.{lang}.md").read_text("utf-8")
        name = meta["name"][lang]
        for target in ("claude", "agents"):
            fm = frontmatter(meta, lang, target)              # REQ-ARCH-77
            text = resolve_tokens(body, lang, target, project_vars)
            root = ".claude/skills" if target == "claude" else ".agents/skills"
            out[f"{root}/{name}/SKILL.md"] = normalize(fm + text).encode("utf-8")
    return out
```

### 10.5 Plantilla

`render_template(src, lang, vars, format)` copia `template/base/`, agrega `template/l10n/<lang>/`, sustituye `{{var:...}}` en los archivos de texto y añade la salida de `render_skills` y de los generadores de `render` (base de `.claude/settings.json`, `.mcp.json`, `.gemini/settings.json` y `.codex/config.toml`, según 07-hooks y §10.3).

- **REQ-ARCH-100.** El render DEBE filtrar por `format`. Con `latex` omite `sections/*/*.md` de las partes. Con `none` omite `tex/**`, `bib/**`, `.latexmkrc`, `sections/*/pending.bib`, `sections/*/*.tex` y, desde H3, `.github/workflows/editor-review.yml` y `editor-review-trigger.yml` (09-ci REQ-CI-57), y escribe `project.yaml` sin `build`, `ci.pdf_retention_days` ni `ci.latexdiff` (02-convencion REQ-CONV-2). `template/l10n/{es,en}/` trae el `<id>.md` de cada una de las 7 partes de 00-marco D-55, y `roles.yaml` con sus títulos en cada idioma, así que REQ-ARCH-83 se sigue cumpliendo.
- **REQ-ARCH-101.** Un archivo de `template/formats/<format>/l10n/<lang>/` reemplaza, en el render de ese formato, al de la misma ruta de `template/l10n/<lang>/`. Solo reemplaza: la ruta DEBE existir en `template/l10n/<lang>/` y entrar en ese render, y `es` y `en` DEBEN traer las mismas variantes; si no, el render falla. Así la `constitution.md` del render `none` cumple 02-convencion REQ-CONV-91.
- **REQ-ARCH-83.** `template/l10n/es/` y `template/l10n/en/` DEBEN contener el mismo conjunto de rutas, y ese conjunto NO DEBE intersectar el de `template/base/`.
- **REQ-ARCH-84.** Los archivos binarios (según detección de UTF-8) DEBEN copiarse sin sustitución.
- **REQ-ARCH-102.** La plantilla completa, con los generados de su `roles.yaml` (`CODEOWNERS` y `task.yml`, 00-marco D-35 j) y `.mcp.json`, DEBE armarla una sola función del núcleo, `render_project`, con los valores de `tools/template_vars.toml`. La usan `tools/render_template.py`, y con él el job `template` de `release.yml` (§2.3) y la autoprueba (00-marco D-37), e `init --repo` (00-marco D-73), que arma la plantilla publicada de la versión instalada: `en`, `latex`, y `core_pin` y `core` de esa versión (REQ-ARCH-44). Así el repositorio plantilla y `init --repo` dan los mismos archivos.

### 10.6 `init` según el idioma

- **REQ-ARCH-85.** `init` DEBE reemplazar cada archivo localizable (ruta presente en `l10n/`) por su versión en `project.yaml: language` solo si su contenido actual, normalizado a LF, es idéntico al render `en` de la misma versión; si difiere, DEBE dejarlo y advertirlo.
- **REQ-ARCH-86.** `init` DEBE renderizar `.claude/skills/` y `.agents/skills/` en el idioma elegido y eliminar los directorios de skills generados en otro idioma (los que tengan `aucove-generated: "true"`).

### 10.7 Actualizaciones de proyectos existentes en v0.1

`upgrade` está reservado. En v0.1 hay tres casos:

| Archivos | Mecanismo en v0.1 |
|---|---|
| Clase `generated` (`.claude/skills/**`, `.agents/skills/**`, `.github/CODEOWNERS`) y la parte de `.claude/settings.json` que genera el núcleo | `aucove init --regenerate` (propuesto, §12), en un PR del responsable |
| Clase `lead` venidos de la plantilla (workflows, `AGENTS.md`, formularios, devcontainer, `.latexmkrc`) | manual: cada release enlaza el diff de `aucove-template` entre etiquetas y el responsable lo aplica por PR |
| Cambio de convención | no existe en v0.1 (solo `"0.1"`); requerirá `upgrade` |

- **REQ-ARCH-87.** `init --regenerate` es operación H (TTY y confirmación), DEBE exigir que la versión instalada sea igual a `core_pin`, NO DEBE llamar a la forja y DEBE limitarse a escribir en el árbol de trabajo los archivos de la primera fila (la mezcla de `.claude/settings.json` la define 07-hooks). Borra solo directorios de skills con `aucove-generated: "true"`.
- **REQ-ARCH-88.** `validate` DEBE comparar los archivos `generated` del árbol, normalizados a LF, con el render de la versión `core_pin` del árbol que se valida, y DEBE fallar listando las rutas que difieren. Las reglas de autorización y seguridad corren con la versión instalada por el workflow de la rama por defecto; `generated-sync`, `roles-sync` y `schemas` se evalúan con el `core_pin` de la cabeza del PR (09-ci REQ-CI-45). Así, subir `core_pin` sin regenerar hace fallar el CI de ese mismo PR.
- **REQ-ARCH-89.** Cambiar `project.yaml: language` y ejecutar `init --regenerate` DEBE producir los skills en el nuevo idioma y borrar los anteriores; los archivos `lead` ya existentes no se tocan.

## 11. Observabilidad local

- **REQ-ARCH-90.** Cada operación, cada decisión de hook, cada reconstrucción de caché y cada error de red DEBEN registrarse como una línea JSON en `.aucove/log/events.jsonl`.
- **REQ-ARCH-91.** Cuando `events.jsonl` supere 1 MiB, DEBE rotarse a `events.1.jsonl` (desplazando hasta `events.3.jsonl` y borrando el más antiguo).
- **REQ-ARCH-92.** Un fallo al registrar NUNCA DEBE cambiar el resultado de una operación ni de un hook.
- **REQ-ARCH-93.** El núcleo NO DEBE enviar telemetría a ningún servicio; el registro es solo local.
- **REQ-ARCH-94.** `AUCOVE_LOG` DEBE aceptar `off`, `info` (por defecto) y `debug`; `debug` solo agrega tiempos por fase, nunca contenido.

Cada línea se escribe con una sola llamada `write` en modo append; los lectores DEBEN ignorar líneas mal formadas (en Windows el append concurrente no está garantizado como atómico).

```json
{"v":1,"ts":"2026-09-29T14:03:12.481Z","core":"0.1.3","kind":"hook","name":"pre-tool-use","result":"deny","code":"<código de 05-operaciones>","tool":"Edit","path":"sections/methods/main.tex","path_class":"section","task":12,"session":"9f86d081884c","ms":41}
```

| Campo | Contenido | Nunca |
|---|---|---|
| `kind` | `op`, `hook`, `cache`, `net`, `compat`, `audit`, `mcp` | — |
| `name` | comando (`claim`), evento de hook o entrada de caché | — |
| `result` | `ok`, `error`, `allow`, `deny`, `skip` | — |
| `code` | código de error de 05-operaciones | texto de excepciones (puede contener URL con tokens) |
| `tool` | nombre de la herramienta de la puerta (`Edit`, `Bash`) | argumentos de la herramienta, comandos de shell |
| `path` | ruta relativa a la raíz, con `/`, solo para herramientas de archivo | rutas absolutas (revelan el usuario del sistema) |
| `session` | primeros 12 hex de SHA-256 del `session_id` | `session_id` o `transcript_path` en claro |
| `net` | host y código HTTP | cabeceras, cuerpos, URL con consulta |
| `ms` | duración | — |

Además, NUNCA se registran: tokens, claves, contraseñas, valores de variables de entorno, contenido de archivos, prompts, títulos o cuerpos de Issues y PR, `login` ni correo.

- **REQ-ARCH-95.** Los campos de un evento DEBEN ser una lista cerrada (la tabla anterior); una prueba DEBE verificar que ningún evento emitido contiene claves fuera de ella.
- **REQ-ARCH-96.** `aucove doctor --report` (propuesto, §12) DEBE escribir en stdout un JSON con la versión del núcleo y de Python, la plataforma, los resultados de `doctor` y las últimas 500 líneas de `events.jsonl`; DEBE funcionar sin red y no DEBE incluir nada de la columna "Nunca".

## 12. Identificadores de este capítulo

| Identificador | Tipo | Definición |
|---|---|---|
| `project.yaml: core` | clave YAML | rango PEP 440 de versiones del núcleo compatibles (§6.2) |
| `project.yaml: core_pin` | clave YAML | versión exacta `X.Y.Z` que instalan CI, devcontainer y nube (§6.2) |
| `aucove init --regenerate` | opción de CLI (H) | re-renderiza los archivos `generated` con la versión fijada (§10.7) |
| `aucove doctor --report` | opción de CLI | paquete de diagnóstico sin contenido (§11) |
| `AUCOVE_LANG` | variable de entorno | idioma de los mensajes en tiempo de ejecución, `es` o `en` (§9.2) |
| `AUCOVE_OFFLINE` | variable de entorno | `1` fuerza el modo sin red (§8.1) |
| `AUCOVE_LOG` | variable de entorno | `off`, `info` o `debug` (§11) |
| `E_CONFIG_CORE_MISMATCH` | código de error | versión instalada fuera de `core` (§6) |
| `E_CONFIG_CONVENTION_UNSUPPORTED` | código de error | convención no soportada por el núcleo (§6) |
| `E_NET_OFFLINE`, `E_NET_TIMEOUT`, `E_NET_TLS` | códigos de error | clasificación de fallas de red (§8.1) |
| `.aucove/.gitignore`, `.aucove/cache/{policy,identity,capabilities,forge-snapshot,local-rules}.json`, `.aucove/cache/tasks/<n>.json`, `.aucove/cache/http/`, `.aucove/state/{heartbeat,notices}.json`, `.aucove/state/sessions/<session_id>.json`, `.aucove/state/worktrees/<id>/{pending,session-state}.json`, `.aucove/work/{quarantine,dropped,snapshots}/<id>/…`, `.aucove/locks/<clave>.lock`, `.aucove/log/events.jsonl`, `.aucove/review/`, `.aucove/tmp/` | rutas locales | estructura de la caché, el estado, el trabajo recuperable y el registro (§7.3, §11) |
| `requirements-lock.txt`, `sbom.cdx.json` | archivos de la GitHub Release | lock con versiones exactas y hashes, y SBOM CycloneDX 1.5 (§2.3, §5.1); el lock se resuelve con el extra `evidence` incluido, y el SBOM marca las licencias LGPL de ese extra |
| `aucove-template` | repositorio | repositorio plantilla publicado (§2.3) |
| `aucove-generated`, `aucove-source` | claves de `metadata` en `SKILL.md` (prefijo derivado de `_meta.NAME`) | marcan un skill generado y su fuente (§10.3) |
| `{{tool:…}}`, `{{cli:…}}`, `{{human:…}}`, `{{skill:…}}`, `{{var:…}}` | marcadores de render | §10.2 |
| `skills/<key>/skill.toml`, `body.<lang>.md`; `template/base/`, `template/l10n/<lang>/`, `template/formats/<format>/l10n/<lang>/`; `tests/{unit,render,hooks,perf,contract,race,mcp,skills,e2e,model,fixtures}/`, `evals/coordinator/cases/`, `docs/acceptance/v<X.Y.Z>.md` | rutas del framework | §2.1, §10 |
| `live`, `model` | marcadores de pytest | pruebas contra la forja real y con modelo (REQ-ARCH-5) |
| `op`, `hook`, `cache`, `net`, `compat`, `audit`, `mcp` | valores de `kind` del registro local | §11 |
| `origin` | nombre del remoto git | remoto que leen `vcs` y la ruta caliente (supuesto; 08-forja puede cambiarlo) |

## 13. Supuestos sobre otros capítulos

- **02-convencion:** `.gitignore` de la plantilla incluye `.aucove/`; el devcontainer cumple REQ-ARCH-36/37; `AGENTS.md` incluye la instrucción de REQ-ARCH-40; las rutas localizables de la plantilla son las de `template/l10n/`.
- **03-esquemas:** `project.yaml` define `convention` (cadena), `core` (cadena PEP 440), `core_pin` (cadena `X.Y.Z`), `language` (`es`|`en`), `forge.type` (`github` en v0.1) y `forge.base_branch`.
- **05-operaciones:** es dueño del sobre, del catálogo de errores (incorpora los códigos de §12), de los códigos de salida y del formato de las advertencias, que admite un campo `as_of`.
- **06-permisos / 07-hooks:** los nombres de evento incluyen `pre-tool-use` y `session-start`; la versión mínima de Claude Code es 2.1.251 (requerida para `PostModelSwitch`, 07-hooks); todos los hooks usan forma exec con `command: "aucove"` y `args: ["hook", "<evento>"]`; definen cómo se bloquea ante incompatibilidad o falta de `project.yaml`, el orden de prioridad para truncar `context`, la regla de mezcla de `.claude/settings.json`, el contenido de `.mcp.json` (con `command: "aucove"`, `args: ["mcp", "--stdio"]`) y las reglas deny estáticas que funcionan aunque el núcleo no esté instalado.
- **08-forja:** define almacenamiento de credenciales, `identity.json`, `capabilities.json`, reintentos, autenticación en sesiones en la nube y el remoto `origin`.
- **09-ci:** el CI instala el `core_pin` del `project.yaml` de la política (09-ci REQ-CI-12).
- **10-seguridad:** define firma de etiquetas y SBOM del release, y aprueba los permisos del token de `template-publish`.
- **12-mvp:** incorpora la prueba de latencia de §7.2 y la matriz de plataformas en sus criterios de aceptación.

## 14. Preguntas abiertas

- **Q-ARCH-1.** OpenCode lee `.claude/skills/` y `.agents/skills/` y exige nombres únicos entre ubicaciones. ¿Se acepta el duplicado en OpenCode (mejor esfuerzo), o se documenta que OpenCode use solo una de las dos rutas? Lo mismo aplica a VS Code con GitHub Copilot, que también lee las dos rutas (§10.1).
- **Q-ARCH-3.** El setup script de la nube fija un solo `PIN` por entorno. Si una persona trabaja en varios proyectos con distinto `core_pin`, ¿un entorno por proyecto o un rango compartido?
- **Q-ARCH-5.** Como la compatibilidad se evalúa con el `project.yaml` de la base (REQ-ARCH-46), el responsable que instala la nueva versión menor antes de fusionar el PR que sube `core` tendrá los hooks cerrados en su propia sesión. ¿Se acepta (edita `project.yaml` a mano) o se permite leer `core` del árbol de trabajo en la rama que lo modifica?
- **Q-ARCH-6.** ¿La plantilla publicada debe estar en `en` (actual) o en `es`? Solo cambia qué idioma no requiere reemplazos en `init`.
- **Q-ARCH-7.** ¿Hace falta un ejecutable autónomo para el piloto en Windows sin uv? Si se hace, tendría que ser de tipo *onedir* y cumplir §7.1.
- **Q-ARCH-8.** En Windows, `uv tool update-shell` cambia el `PATH` del usuario. Hay que verificar si Claude Desktop (lanzado desde el Explorador) lo ve sin cerrar sesión.
- **Q-ARCH-9.** ¿Es alcanzable el p95 < 300 ms con Microsoft Defender activo en equipos institucionales? Conviene medirlo en la semana 1 del desarrollo; si no se cumple, las alternativas son precompilar los catálogos a módulos Python o reducir aún más los imports.

## 15. Referencias

Verificadas el 2026-09-29:

- Claude Code, hooks: forma exec (`args`), requisito de `.exe` en Windows, tope de 10 000 caracteres, timeout de 600 s, matchers de SessionStart, hook que no arranca es no bloqueante, `CLAUDE_CODE_REMOTE`, `CLAUDE_PROJECT_DIR`. https://code.claude.com/docs/en/hooks
- Claude Code, skills: ubicaciones (sin `.agents/skills/`), precedencia, campos del frontmatter, límite de 1536 caracteres, frontmatter en la primera línea, `metadata`. https://code.claude.com/docs/en/skills
- Claude Code en la nube: GitHub, superficies. https://code.claude.com/docs/en/claude-code-on-the-web
- Entornos en la nube: setup scripts (root, Ubuntu 24.04, salida 0, caché), dominios Trusted (PyPI), herramientas instaladas (uv), qué se conserva del repo, proxy de GitHub y alcance de *release assets*. https://code.claude.com/docs/en/cloud-environments
- Agent Skills, especificación (`name`, `description`, `metadata`, tamaño del cuerpo). https://agentskills.io/specification
- Codex, skills (`.agents/skills` hasta la raíz del repo). https://developers.openai.com/codex/skills
- OpenCode, skills (lee `.claude/skills` y `.agents/skills`). https://opencode.ai/docs/skills/
- Gemini CLI, skills (`.agents/skills/` como alias de `.gemini/skills/`). https://geminicli.com/docs/cli/skills/
- Claude Code, comandos integrados y skills incluidos (REQ-ARCH-78). https://code.claude.com/docs/en/commands
- uv: herramientas y `update-shell` https://docs.astral.sh/uv/concepts/tools/ · directorios https://docs.astral.sh/uv/reference/storage/ · variables `UV_COMPILE_BYTECODE`, `UV_PYTHON_DOWNLOADS`, `UV_TOOL_BIN_DIR`, `UV_SYSTEM_CERTS`, `SSL_CERT_FILE` https://docs.astral.sh/uv/reference/environment/ · instaladores con versión https://docs.astral.sh/uv/getting-started/installation/ · trampolines `.exe` en Windows https://github.com/astral-sh/uv/tree/main/crates/uv-trampoline
- SDK MCP de Python v2 (2.2.0, 2026-09-07; `MCPServer`; revisión 2026-07-28 y anteriores). https://pypi.org/project/mcp/ · https://github.com/modelcontextprotocol/python-sdk
- SDK MCP 2.2.0, dependencias declaradas (`requires_dist`): `anyio`, `httpx2>=2.5.0`, `jsonschema>=4.20.0`, `mcp-types==2.2.0`, `opentelemetry-api`, `pydantic>=2.12.0`, `pyjwt[crypto]`, `python-multipart`, `pywin32` (solo Windows), `sse-starlette`, `starlette`, `typing-extensions`, `typing-inspection` y `uvicorn`; extras `cli` (`python-dotenv`, `typer`) y `rich`. Los módulos que importan son los que excluye REQ-ARCH-26. https://pypi.org/pypi/mcp/2.2.0/json
- httpx2 (2.13.1). https://pypi.org/project/httpx2/
- GitHub REST, `is_template` y `POST .../generate`. https://docs.github.com/en/rest/repos/repos
- GitHub Apps: el permiso de repositorio `Workflows` es necesario para crear o editar archivos de `.github/workflows/` (REQ-ARCH-10). https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app
- GitHub, repositorios plantilla (un solo commit, historias no relacionadas, sin Git LFS). https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-template-repository · https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-repository-from-a-template
- Trusted Publishing y atestaciones. https://github.com/pypa/gh-action-pypi-publish
- Hatch, `force-include`. https://hatch.pypa.io/latest/config/build/
- truststore. https://truststore.readthedocs.io/en/latest/
- jsonschema, `referencing.Registry` sin recuperación remota. https://python-jsonschema.readthedocs.io/en/stable/referencing/
- import-linter, tipos de contrato. https://import-linter.readthedocs.io/en/stable/contract_types/

Supuestos no verificados en fuente oficial:

- El refresco del `PATH` de Claude Desktop en Windows (Q-ARCH-8).
