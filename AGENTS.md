# AGENTS.md

Instrucciones para agentes de IA que trabajan en el repositorio del framework *aucove*. Las personas encuentran las mismas reglas, con más detalle, en [CONTRIBUTING.md](CONTRIBUTING.md). An English summary is at the end.

## Si la persona quiere usar aucove

Este repositorio es para desarrollar aucove. Si la persona quiere usarlo en un proyecto suyo, díselo y no sigan aquí: las reglas de abajo son para desarrollar el framework, y este clon no tiene las herramientas de un proyecto. Que instale aucove (README, «Instalación»), cree en GitHub un repo vacío y, en una carpeta vacía, corra `aucove init --repo <org>/<repo>` en una terminal, fuera del agente; después, que abra su agente en esa carpeta. Lo explica la [guía del responsable](docs/es/responsable.md).

## Commits

- **No agregues `Signed-off-by`.** Solo lo agrega la persona, con `git commit -s`, después de revisar y probar el cambio (11-trazabilidad REQ-TRACE-66 y REQ-TRACE-67).
- **Agrega el trailer `Assisted-by: <tool>:<model>`** a todo commit que redactes o en el que hayas ayudado (REQ-TRACE-68). `tool` es un slug en minúsculas (`claude-code`, `codex`, `antigravity-cli`, `gemini-cli`, `opencode`, `goose` u `other`) y `model`, el identificador del modelo tal como lo da su fuente, o `unknown`. Una línea por herramienta y modelo, ordenadas y sin duplicados, en el último párrafo del mensaje y antes de `Signed-off-by`. Ejemplo: `Assisted-by: claude-code:claude-opus-5-5`.
- **No agregues `Co-authored-by`** para una herramienta o un modelo de IA: la IA no es autora.
- No hagas commits, `push` ni `git config`. Deja el cambio verificado y el mensaje del commit listos: la persona revisa, firma con `git commit -s` y sube.

## Fuente de verdad

- La especificación v0.1 es la autoridad normativa y vive en `spec/` (00-marco D-51); `schemas/` es una copia verificada de `spec/schemas/` y no se edita a mano.
- Si el código y la especificación no coinciden, sigue la especificación y avisa a la persona.

## Código y archivos

- Python 3.11 o posterior, con anotaciones de tipo. Identificadores en inglés ASCII (00-marco D-02); comentarios en español o en inglés.
- El nombre `aucove` se escribe solo en `src/aucove/_meta.py`; el resto de `src/` lo deriva de `NAME` (01-arquitectura REQ-ARCH-19).
- Texto en UTF-8 sin BOM, con LF y salto de línea final.
- Cada `.py` de `src/`, `tools/` y `tests/` empieza con las dos líneas SPDX de 11-trazabilidad REQ-TRACE-56 (copyright de «2026 The aucove Authors» y licencia Apache-2.0); `tests/unit/test_spdx_headers.py` lo comprueba. Los demás archivos no llevan cabeceras: los cubre `REUSE.toml`.
- Los fixtures no contienen `REUSE.toml`, carpetas `LICENSES/`, archivos `.license` ni etiquetas SPDX literales. El código de pruebas que mencione esas etiquetas como dato las arma por concatenación.
- El núcleo no importa `reuse` (GPL-3.0-or-later; REQ-TRACE-59). `reuse` se ejecuta desde `.venv-tools`.
- Los skills se escriben solo en `skills/`; el repositorio plantilla sale solo de `template/`; `deploy/` no contiene código (REQ-ARCH-1, REQ-ARCH-2, REQ-ARCH-4).

## Pruebas

- `.venv\Scripts\python -m pytest -m "not live and not model" -q` (en Linux o macOS, `.venv/bin/python`).
- `.venv-tools\Scripts\reuse lint` debe terminar sin errores.
- Las pruebas unitarias no usan la red, no ejecutan `git config` y no hacen commits en este repositorio: usan repositorios temporales con la identidad en variables de entorno.

## English summary

- If the person wants to use aucove in a project of their own, tell them and stop here: these rules are for developing the framework. They install aucove, create an empty GitHub repository, run `aucove init --repo <org>/<repo>` in an empty folder from a terminal outside the agent, and then open their agent in that folder ([lead guide](docs/en/lead.md)).
- Never add `Signed-off-by`; only the person adds it with `git commit -s` after reviewing and testing the change.
- Add `Assisted-by: <tool>:<model>` (for example, `Assisted-by: claude-code:claude-opus-5-5`) to every commit you write or help with, in the last paragraph and before `Signed-off-by`.
- Never add `Co-authored-by` for an AI tool or model.
- Do not commit, push or run `git config`: leave the verified change and the commit message ready; the person reviews, signs with `git commit -s` and pushes.
- Follow the specification, the SPDX header rule (11-trazabilidad REQ-TRACE-56) and `reuse lint`; see [CONTRIBUTING.md](CONTRIBUTING.md).
