# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Operaciones de varios pasos: estado, fallas, reflejos, plazo y ``--dry-run`` (05 §6.3 a §6.5).

Cada operación declara sus pasos en el orden de 05 §9 y los ejecuta con ``Steps``:

- ``run`` ejecuta un paso canónico o local. Si falla (``OpError`` o ``ForgeError``, este con
  la traducción de 05 §12.3), la operación termina con ``ok=false`` y ``data`` =
  ``failure_data`` (REQ-OPS-24): el paso que falló y el estado de cada uno. Un ``OpError``
  que ya trae ``data`` (p. ej. ``foreign_sections`` de P3) la conserva junto a esos campos.
- ``reflect`` ejecuta un paso reflejo: si la forja falla, el paso queda ``failed``, la
  operación sigue y termina con ``W_FORGE_REFLECTION_FAILED`` (REQ-OPS-25).
- Antes de cada paso, ``checkpoint`` aplica el plazo de 60 s de la CLI y de MCP
  (REQ-OPS-22) y la cancelación de MCP (REQ-OPS-55): se detiene en el borde, nunca a media
  escritura, con ``E_NET_TIMEOUT`` y ``failure_data``.
- Con ``--dry-run`` (REQ-OPS-30) las lecturas y las verificaciones corren igual, y en lugar
  de escribir cada paso agrega a ``plan`` el efecto que tendría.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Literal, TypeVar

from ..forge.base import ForgeError
from ..i18n import Translator
from .registry import forge_error_code
from .result import OpError, Result, WarningObj, make_warning, ok

if TYPE_CHECKING:
    from .context import OpContext

__all__ = ["DEADLINE_S", "EffectKind", "StepStatus", "Steps"]

DEADLINE_S = 60.0  # REQ-OPS-22: CLI con TTY y MCP
_INTERACTIVE = frozenset({"cli", "mcp"})

StepStatus = Literal["done", "skipped", "failed", "pending", "compensated"]
EffectKind = Literal[
    "forge_create", "forge_update", "forge_delete", "git_commit", "git_push", "git_checkout", "git_merge",
    "file_write", "local_write",
]
T = TypeVar("T")


class Steps:
    """Pasos ordenados de una ejecución de la operación ``op``."""

    def __init__(self, ctx: OpContext, op: str, ids: Sequence[str], *, deadline_s: float | None = DEADLINE_S) -> None:
        if len(set(ids)) != len(ids):
            raise ValueError("ids de paso repetidos")
        self.ctx = ctx
        self.op = op
        self._order = list(ids)
        self._status: dict[str, StepStatus] = dict.fromkeys(ids, "pending")
        self._detail: dict[str, str | None] = {}
        self.failed_reflections: list[str] = []
        self.plan: list[dict[str, str]] = []
        interactive = ctx.inv.surface in _INTERACTIVE
        self._deadline = time.monotonic() + deadline_s if deadline_s is not None and interactive else None

    @property
    def dry_run(self) -> bool:
        return self.ctx.inv.dry_run

    def status(self, step: str) -> StepStatus:
        return self._status[step]

    def _set(self, step: str, status: StepStatus, detail: str | None = None) -> None:
        if step not in self._status:
            raise KeyError(f"paso no declarado: {step!r}")
        self._status[step] = status
        if detail is not None:
            self._detail[step] = detail

    def done(self, step: str, detail: str | None = None) -> None:
        self._set(step, "done", detail)

    def skip(self, step: str, detail: str | None = None) -> None:
        self._set(step, "skipped", detail)

    def compensated(self, step: str, detail: str | None = None) -> None:
        self._set(step, "compensated", detail)

    def as_list(self) -> list[dict[str, object]]:
        """``steps`` del resultado: un ``$defs/step`` por paso, en el orden declarado."""
        out: list[dict[str, object]] = []
        for step in self._order:
            item: dict[str, object] = {"id": step, "status": self._status[step]}
            if step in self._detail:
                item["detail"] = self._detail[step]
            out.append(item)
        return out

    def failure_data(self, failed_step: str, extra: Mapping[str, object] | None = None) -> dict[str, object]:
        """``failure_data`` (05 §6.3) con los campos de ``extra`` que no sean los suyos."""
        data: dict[str, object] = {k: v for k, v in (extra or {}).items() if k not in ("failed_step", "steps")}
        data["failed_step"] = failed_step
        data["steps"] = self.as_list()
        return data

    def failure(self, step: str, code: str, *, details: dict | None = None,  # type: ignore[type-arg]
                data: Mapping[str, object] | None = None) -> OpError:
        """``OpError`` de la operación con el paso ``step`` como fallido."""
        self._set(step, "failed")
        return OpError(code, details=details, data=self.failure_data(step, data))

    def _wrap(self, step: str, exc: OpError) -> OpError:
        self._set(step, "failed")
        extra = exc.data if isinstance(exc.data, Mapping) else None
        return OpError(exc.code, details=exc.details, data=self.failure_data(step, extra), warnings=exc.warnings,
                       **exc.fmt)

    def checkpoint(self, step: str) -> None:
        """Borde de un paso: cancelación de MCP (REQ-OPS-55) y plazo (REQ-OPS-22)."""
        if self.ctx.cancelled():
            raise self.failure(step, "E_NET_TIMEOUT", details={"reason": "cancelled"})
        if self._deadline is not None and time.monotonic() > self._deadline:
            raise self.failure(step, "E_NET_TIMEOUT", details={"reason": "deadline", "deadline_s": DEADLINE_S})

    def run(self, step: str, fn: Callable[[], T], detail: str | None = None) -> T:
        """Paso canónico o local: si falla, la operación falla con ``failure_data``."""
        self.checkpoint(step)
        try:
            value = fn()
        except OpError as exc:
            raise self._wrap(step, exc) from exc
        except ForgeError as exc:
            code, details = forge_error_code(exc)
            raise self.failure(step, code, details=details) from exc
        self._set(step, "done", detail)
        return value

    def reflect(self, step: str, fn: Callable[[], T]) -> T | None:
        """Paso reflejo: un fallo de la forja no detiene la operación (REQ-OPS-25)."""
        self.checkpoint(step)
        try:
            value = fn()
        except ForgeError:
            self._set(step, "failed")
            self.failed_reflections.append(step)
            return None
        self._set(step, "done")
        return value

    def effect(self, kind: EffectKind, target: str, summary: str) -> None:
        """Efecto que haría la ejecución real (``--dry-run``, REQ-OPS-30)."""
        self.plan.append({"kind": kind, "target": target, "summary": summary})

    def warnings(self, t: Translator) -> list[WarningObj]:
        """``W_FORGE_REFLECTION_FAILED`` por cada reflejo fallido, con ``details.step``."""
        return [make_warning("W_FORGE_REFLECTION_FAILED", t, details={"step": step}) for step in self.failed_reflections]

    def dry_result(self, warnings: Sequence[WarningObj] = ()) -> Result:
        """Resultado de ``--dry-run``: ``{"dry_run": true, "plan": [...]}``."""
        return ok(self.op, {"dry_run": True, "plan": list(self.plan)}, warnings)
