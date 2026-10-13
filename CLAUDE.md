# CLAUDE.md

Las reglas para agentes de este repositorio están en `AGENTS.md`, que Claude Code importa aquí:

@AGENTS.md

Además, para Claude Code:

- `.claude/settings.json` fija `"attribution": {"commit": ""}`, para que Claude Code no agregue `Co-Authored-By` a los commits (11-trazabilidad REQ-TRACE-68). No cambies esa clave.
- En su lugar, un commit que redactes lleva `Assisted-by: claude-code:<model>`, con el identificador del modelo en uso, y el `Signed-off-by` lo agrega la persona.
