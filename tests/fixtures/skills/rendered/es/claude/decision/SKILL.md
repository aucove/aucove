---
name: "decision"
description: "Propón una decisión: redacta el registro en memory/decisions/ y abre la propuesta para que la apruebe el responsable."
license: "MIT-0"
disable-model-invocation: true
argument-hint: "[tema de la decisión]"
metadata:
  ${NAME}-generated: "true"
  ${NAME}-source: "decision"
---
# Proponer una decisión

1. Confirma que puedes crear el registro con `mcp__${NAME}__check_path`.
2. Redacta el registro en `memory/decisions/` con `${NAME} decision propose`.
3. Para ver cómo queda todo, usa `/estado`.
4. La aprobación es humana: `${NAME} review approve` (solo en tu terminal; el agente no puede ejecutarla).
