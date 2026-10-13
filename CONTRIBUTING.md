# Contribuir a aucove / Contributing to aucove

[Español](#español) · [English](#english)

## Español

### Antes de empezar

- La especificación v0.1 es la autoridad normativa y vive en [`spec/`](spec/) (00-marco D-51). `schemas/` es una copia verificada de `spec/schemas/`: se actualiza con `python tools/sync_schemas.py --spec-dir spec`, nunca a mano. Las pruebas que se comparan con la spec la toman de `spec/`; `AUCOVE_SPEC_DIR` solo hace falta para probar otra copia.
- Se aceptan contribuciones por PR, con las reglas de esta guía. Para un cambio grande, abre antes un Issue y acuerda el enfoque. El titular de los derechos está en [NOTICE](NOTICE).
- Las discusiones y revisiones siguen el [código de conducta](CODE_OF_CONDUCT.md). Las vulnerabilidades se reportan como indica [SECURITY.md](SECURITY.md), nunca en un Issue público.

### DCO: `Signed-off-by` obligatorio

Todo commit de un PR a este repositorio lleva la línea `Signed-off-by` del [Developer Certificate of Origin 1.1](https://developercertificate.org/) (11-trazabilidad REQ-TRACE-66). Con ella certificas que tienes derecho a enviar el cambio bajo la licencia del repositorio.

- El nombre y el correo de `Signed-off-by` DEBEN ser los mismos que los del autor del commit. `git commit -s` la agrega con tu `user.name` y tu `user.email`.
- Un check del CI lo verifica en cada commit del PR.
- Están exentos los PR que la API de GitHub atribuye a `dependabot[bot]`: cuenta el usuario que abre el PR, no el correo del commit, que se puede falsificar. Quien integra un PR de Dependabot responde por ese cambio (00-marco D-07 c).
- El repositorio exige además la firma en los commits hechos desde la web de GitHub (*require contributors to sign off on web-based commits*).

### Contribuciones asistidas por IA

Puedes usar herramientas de IA, con estas reglas (11-trazabilidad REQ-TRACE-67):

1. **Los agentes de IA NO DEBEN agregar `Signed-off-by`.** Solo lo agrega la persona, porque el DCO es una declaración suya.
2. **La persona revisa y prueba todo lo generado y responde por ello**, igual que si lo hubiera escrito ella.
3. **Los commits asistidos llevan `Assisted-by: <tool>:<model>`**, una línea por herramienta y modelo, con la gramática de 11-trazabilidad §2.2:

   ```abnf
   assisted-tr   = %s"Assisted-by: " slug ":" model LF          ; slug de herramienta, máx. 32
   model         = 1*128(ALPHA / DIGIT / "." / "_" / "-" / ":" / "@" / "/" / "+")
   slug          = 1*(%x61-7A / DIGIT) *("-" 1*(%x61-7A / DIGIT))
   ```

   - `tool` es un slug en minúsculas: `claude-code`, `codex`, `antigravity-cli`, `gemini-cli`, `opencode`, `goose` u `other`.
   - `model` es el identificador del modelo tal como lo da su fuente (por ejemplo, `claude-opus-5-5`), o `unknown` si no lo sabes. No lo cambies por un nombre comercial.
   - El separador es el primer `:`; el modelo puede contener más `:`.
   - Ordena las líneas por herramienta y luego por modelo, sin duplicados.
4. **La IA no figura como `Co-authored-by`.** Si tu herramienta agrega ese trailer, quítalo. En este repositorio, `.claude/settings.json` fija `"attribution": {"commit": ""}` para que Claude Code no lo agregue (REQ-TRACE-68).

Los trailers van en el último párrafo del mensaje, separados del texto por una línea en blanco, con `Assisted-by` antes de `Signed-off-by`:

```text
Agrega la regla de alcance para renombres

Assisted-by: claude-code:claude-opus-5-5
Signed-off-by: Ana Pérez <ana.perez@example.org>
```

### Convenciones de código y de archivos

- Python 3.11 o posterior, con anotaciones de tipo.
- Identificadores estables en inglés ASCII; comentarios y documentación en español o inglés (00-marco D-02).
- El nombre `aucove` se escribe solo en `src/aucove/_meta.py`; el resto del código lo deriva de `NAME` (01-arquitectura REQ-ARCH-19).
- Texto en UTF-8 sin BOM, con LF y salto de línea final (`.gitattributes` fija `eol=lf`).
- Cada `.py` de `src/`, `tools/` y `tests/` empieza con estas dos líneas (11-trazabilidad REQ-TRACE-56):

  <!-- REUSE-IgnoreStart -->
  ```python
  # SPDX-FileCopyrightText: 2026 The aucove Authors
  # SPDX-License-Identifier: Apache-2.0
  ```
  <!-- REUSE-IgnoreEnd -->

- Los demás archivos no llevan cabeceras: su licencia está en `REUSE.toml` (y en `template/base/REUSE.toml` para `template/base/**`). Los fixtures de `tests/fixtures/` no contienen `REUSE.toml`, carpetas `LICENSES/`, archivos `.license` ni etiquetas SPDX literales; la prueba que los necesite los genera en un directorio temporal.
- `LICENSES/` tiene exactamente los textos que se usan (REUSE prohíbe los que sobran), sin cambios y en UTF-8 con LF. `Apache-2.0.txt` es idéntico byte a byte a `LICENSE`, el texto canónico de la ASF (<https://www.apache.org/licenses/LICENSE-2.0.txt>). `CC0-1.0.txt` y `MIT-0.txt` son idénticos byte a byte a los de `template/base/LICENSES/`, y se cambian siempre juntos. Las pruebas comprueban las dos igualdades. Un texto nuevo se copia sin cambios de `text/<id>.txt` de [spdx/license-list-data](https://github.com/spdx/license-list-data). Descárgalo en binario, por ejemplo con `curl -fsSL -o LICENSES/<id>.txt https://raw.githubusercontent.com/spdx/license-list-data/main/text/<id>.txt`: en Windows, `reuse download` lo escribe con CRLF.
- El núcleo no importa el paquete `reuse`, que incluye código GPL-3.0-or-later; solo se invoca como herramienta externa (REQ-TRACE-59).
- `skills/` es la única fuente de los skills, `template/` la única fuente del repositorio plantilla y `deploy/` no contiene código en v0.1 (REQ-ARCH-1, REQ-ARCH-2, REQ-ARCH-4).

### Entorno local

El entorno local no necesita `gh`, LaTeX ni Docker: las pruebas que los usan (`docker`, `live`) se omiten solas, y la compilación, `actionlint` y la matriz de sistemas corren en el CI. Hace falta [uv](https://docs.astral.sh/uv/). Comandos en PowerShell, desde la raíz del repositorio:

```powershell
# 1. Entorno de desarrollo con las versiones de uv.lock, como en el CI (Python 3.11 o posterior).
uv sync --locked --group dev --extra evidence --no-install-project
uv sync --locked --group dev --extra evidence --no-build-isolation-package aucove
$env:PATH = "$PWD\.venv\Scripts;$env:PATH"                 # para que las pruebas encuentren el lanzador

# 2. reuse va en un entorno aparte, porque es GPL-3.0-or-later y el núcleo no lo importa.
python -m venv .venv-tools
.venv-tools\Scripts\python -m pip install reuse==6.2.0

# 3. Pruebas y comprobaciones.
.venv\Scripts\python -m pytest -m "not live and not model" -q
.venv\Scripts\lint-imports
.venv-tools\Scripts\reuse lint
```

En Linux o macOS, usa `.venv/bin/` en lugar de `.venv\Scripts\` y `export` para las variables.

Reglas de las pruebas:

- Las pruebas unitarias no usan la red; un fixture la bloquea.
- Ninguna prueba ejecuta `git config` ni hace commits en este repositorio. Los repositorios de prueba son temporales, la identidad va por variables de entorno (`GIT_AUTHOR_NAME`, `GIT_COMMITTER_EMAIL`...) y la configuración global se neutraliza con `GIT_CONFIG_GLOBAL` y `GIT_CONFIG_NOSYSTEM=1`.

### Aviso de no afiliación

> *aucove* es un proyecto independiente de código abierto. No está afiliado a Anthropic, OpenAI, Google, GitHub ni a otros proveedores mencionados, ni cuenta con su respaldo o patrocinio. Claude, Claude Code, Codex, Gemini, GitHub y las demás marcas pertenecen a sus titulares y se mencionan solo para indicar compatibilidad.

## English

### Before you start

- The v0.1 specification is the normative authority and lives in [`spec/`](spec/) (00-marco D-51). `schemas/` is a verified copy of `spec/schemas/`: update it with `python tools/sync_schemas.py --spec-dir spec`, never by hand. The tests that compare with the spec take it from `spec/`; `AUCOVE_SPEC_DIR` is only needed to test another copy.
- Contributions are welcome through pull requests, under the rules in this guide. For a large change, open an issue first and agree on the approach. The copyright holder is stated in [NOTICE](NOTICE).
- Discussions and reviews follow the [code of conduct](CODE_OF_CONDUCT.md). Vulnerabilities are reported as described in [SECURITY.md](SECURITY.md), never in a public issue.

### DCO: `Signed-off-by` is required

Every commit in a pull request to this repository carries the `Signed-off-by` line of the [Developer Certificate of Origin 1.1](https://developercertificate.org/) (11-trazabilidad REQ-TRACE-66). With it you certify that you have the right to submit the change under the repository's license.

- The name and email in `Signed-off-by` MUST match the commit author's. `git commit -s` adds it with your `user.name` and `user.email`.
- A CI check verifies every commit of the pull request.
- Pull requests that the GitHub API attributes to `dependabot[bot]` are exempt: what counts is the user who opens the pull request, not the commit email, which can be forged. Whoever merges a Dependabot pull request is responsible for that change (00-marco D-07 c).
- The repository also requires sign-off on commits made in the GitHub web interface (*require contributors to sign off on web-based commits*).

### AI-assisted contributions

You may use AI tools, under these rules (11-trazabilidad REQ-TRACE-67):

1. **AI agents MUST NOT add `Signed-off-by`.** Only the person adds it, because the DCO is their own statement.
2. **The person reviews and tests everything that was generated and is responsible for it**, as if they had written it.
3. **Assisted commits carry `Assisted-by: <tool>:<model>`**, one line per tool and model, following the grammar of 11-trazabilidad §2.2:

   ```abnf
   assisted-tr   = %s"Assisted-by: " slug ":" model LF          ; tool slug, max. 32
   model         = 1*128(ALPHA / DIGIT / "." / "_" / "-" / ":" / "@" / "/" / "+")
   slug          = 1*(%x61-7A / DIGIT) *("-" 1*(%x61-7A / DIGIT))
   ```

   - `tool` is a lowercase slug: `claude-code`, `codex`, `antigravity-cli`, `gemini-cli`, `opencode`, `goose` or `other`.
   - `model` is the model identifier as given by its source (for example, `claude-opus-5-5`), or `unknown` if you do not know it. Do not replace it with a commercial name.
   - The separator is the first `:`; the model may contain more `:` characters.
   - Sort the lines by tool and then by model, without duplicates.
4. **AI is not listed as `Co-authored-by`.** If your tool adds that trailer, remove it. In this repository, `.claude/settings.json` sets `"attribution": {"commit": ""}` so that Claude Code does not add it (REQ-TRACE-68).

Trailers go in the last paragraph of the message, separated from the text by a blank line, with `Assisted-by` before `Signed-off-by`:

```text
Add the scope rule for renames

Assisted-by: claude-code:claude-opus-5-5
Signed-off-by: Ana Pérez <ana.perez@example.org>
```

### Code and file conventions

- Python 3.11 or later, with type annotations.
- Stable identifiers in ASCII English; comments and documentation in Spanish or English (00-marco D-02).
- The name `aucove` is written only in `src/aucove/_meta.py`; the rest of the code derives it from `NAME` (01-arquitectura REQ-ARCH-19).
- Text in UTF-8 without BOM, with LF and a final newline (`.gitattributes` sets `eol=lf`).
- Every `.py` file in `src/`, `tools/` and `tests/` starts with these two lines (11-trazabilidad REQ-TRACE-56):

  <!-- REUSE-IgnoreStart -->
  ```python
  # SPDX-FileCopyrightText: 2026 The aucove Authors
  # SPDX-License-Identifier: Apache-2.0
  ```
  <!-- REUSE-IgnoreEnd -->

- Other files carry no headers: their license is in `REUSE.toml` (and in `template/base/REUSE.toml` for `template/base/**`). The fixtures in `tests/fixtures/` contain no `REUSE.toml`, `LICENSES/` folders, `.license` files or literal SPDX tags; a test that needs them generates them in a temporary directory.
- `LICENSES/` holds exactly the texts in use (REUSE forbids unused ones), unchanged, in UTF-8 with LF. `Apache-2.0.txt` is byte-for-byte identical to `LICENSE`, the canonical ASF text (<https://www.apache.org/licenses/LICENSE-2.0.txt>). `CC0-1.0.txt` and `MIT-0.txt` are byte-for-byte identical to the ones in `template/base/LICENSES/`, and they always change together. The tests check both equalities. A new text is copied unchanged from `text/<id>.txt` in [spdx/license-list-data](https://github.com/spdx/license-list-data). Download it in binary mode, for example with `curl -fsSL -o LICENSES/<id>.txt https://raw.githubusercontent.com/spdx/license-list-data/main/text/<id>.txt`: on Windows, `reuse download` writes it with CRLF.
- The core does not import the `reuse` package, which includes GPL-3.0-or-later code; it is only invoked as an external tool (REQ-TRACE-59).
- `skills/` is the single source of the skills, `template/` the single source of the template repository, and `deploy/` contains no code in v0.1 (REQ-ARCH-1, REQ-ARCH-2, REQ-ARCH-4).

### Local environment

The local environment does not need `gh`, LaTeX or Docker: the tests that use them (`docker`, `live`) skip themselves, and compilation, `actionlint` and the OS matrix run in CI. You need [uv](https://docs.astral.sh/uv/). PowerShell commands, from the repository root:

```powershell
# 1. Development environment with the versions in uv.lock, as in CI (Python 3.11 or later).
uv sync --locked --group dev --extra evidence --no-install-project
uv sync --locked --group dev --extra evidence --no-build-isolation-package aucove
$env:PATH = "$PWD\.venv\Scripts;$env:PATH"                 # so that the tests find the launcher

# 2. reuse goes in a separate environment, because it is GPL-3.0-or-later and the core does not import it.
python -m venv .venv-tools
.venv-tools\Scripts\python -m pip install reuse==6.2.0

# 3. Tests and checks.
.venv\Scripts\python -m pytest -m "not live and not model" -q
.venv\Scripts\lint-imports
.venv-tools\Scripts\reuse lint
```

On Linux or macOS, use `.venv/bin/` instead of `.venv\Scripts\` and `export` for the variables.

Test rules:

- Unit tests do not use the network; a fixture blocks it.
- No test runs `git config` or makes commits in this repository. Test repositories are temporary, the identity is passed through environment variables (`GIT_AUTHOR_NAME`, `GIT_COMMITTER_EMAIL`...) and the global configuration is neutralized with `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_NOSYSTEM=1`.

### Non-affiliation notice

> *aucove* is an independent open source project. It is not affiliated with, endorsed by, or sponsored by Anthropic, OpenAI, Google, GitHub, or any other provider mentioned. Claude, Claude Code, Codex, Gemini, GitHub, and other marks belong to their respective owners and are mentioned only to indicate compatibility.
