---
name: "decision"
description: "Propose a decision: draft the record in memory/decisions/ and open the proposal for the lead to approve."
license: "MIT-0"
disable-model-invocation: true
argument-hint: "[decision topic]"
metadata:
  ${NAME}-generated: "true"
  ${NAME}-source: "decision"
---
# Propose a decision

1. Confirm that you may create the record with `mcp__${NAME}__check_path`.
2. Draft the record in `memory/decisions/` with `${NAME} decision propose`.
3. To see where everything stands, use `/project-status`.
4. Approval is a human step: `${NAME} review approve` (only in your terminal; the agent cannot run it).
