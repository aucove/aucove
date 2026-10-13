# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Operación ``evidence verify <ruta>`` (05-operaciones §9.26; 11-trazabilidad §8.6; D-50).

La usa cualquiera, también fuera de un proyecto: un árbitro con una copia. ``<ruta>`` es
una carpeta de registro (``<kind>-<commit>/`` o, en la copia local, ``<kind>-<commit>-a<n>/``)
o una carpeta que las contiene: un artefacto ``evidence-*``, un paquete o la copia local.
La verificación de cada registro es de ``evidence.verify``; aquí se validan los
argumentos, se arman ``--repo`` y ``--content``, se localizan los detalles y se arma el
sobre (``data_evidence_verify``). Cada check lleva ``reason``, el motivo estable para las
máquinas (``null`` en un ``pass`` sin motivo), y ``detail``, su texto localizado.

- Calendarios: con red y sin ``--offline``, consulta los calendarios para completar en
  memoria las pruebas OTS sin bloque, sin escribir nada; ``--upgrade`` además reescribe
  ``manifest.json.ots`` (salvo con ``--dry-run``, REQ-OPS-13). Una sola corrida pide cada
  par (calendario, mensaje) una vez y hace como mucho 4 solicitudes por calendario y 16 en
  total (``ots.Budget``). Sin red (``--offline`` o ``<PREFIJO>OFFLINE=1``) no los consulta
  y, si queda alguna prueba OTS sin bloque, avisa ``W_NET_OFFLINE``; también lo avisa si
  los calendarios no respondieron. git corre siempre sin red.
- ``--content``: la forma limpia se calcula en un repositorio temporal, sin la
  configuración ni los objetos de esa carpeta (``evidence.verify.CleanForm``).
- Sin el extra ``evidence``, ``tsa`` y ``ots`` quedan en ``skip`` con
  ``W_CONFIG_TOOL_MISSING`` (REQ-ARCH-99); un registro ``verified`` con confianza
  ``weak`` agrega ``W_VALIDATION_EVIDENCE_WEAK``.
- Cada carpeta se verifica aislada: si sus archivos provocan un error inesperado, o el
  registro no se puede identificar, ese registro da ``failed`` (``evidence.verify.
  unverifiable``) y los demás siguen. Si algo falla: ``E_VALIDATION_EVIDENCE_FAILED`` con la
  ``data`` completa. Solo una ruta sin registros, o un argumento que no sirve, es
  ``E_VALIDATION_INPUT``.
- REQ-OPS-65: no tiene herramienta MCP y la salida nunca lleva sales; las rutas solo
  aparecen en los checks ``content``. Sin tope de tamaño: no tiene MCP y su ``data`` no
  admite recorte (05 §12.1).
- Horas en UTC y en ``America/Mexico_City`` o la zona de ``--tz``; sin base de zonas
  (Windows sin ``tzdata``), solo UTC.
- Pruebas: con ``<PREFIJO>TESTING=1``, ``<PREFIJO>EVIDENCE_FAKE`` nombra el mismo archivo
  Python que usa ``ci seal``: su ``catalog()``, si lo define, reemplaza el catálogo de TSA y
  su ``transport()`` lleva a los calendarios de prueba (la autoprueba de D-37, sin red).
  Fuera de las pruebas la variable se ignora.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import _meta
from ..evidence import timefmt
from ..evidence import verify as ev
from ..evidence.catalog import TOOL_MISSING, Catalog
from ..i18n import Translator
from ..vcs.git import Git, GitError, GitTimeout, GitUnavailable
from .context import env_flag, testing
from .result import OpError, Result, WarningObj, clip, make_warning, ok

if TYPE_CHECKING:
    from .context import OpContext

__all__ = ["OP", "render_text", "run"]

OP = "evidence.verify"
MAX_DETAIL = 300  # maxLength de evidence_check.detail
NET_OFFLINE = "W_NET_OFFLINE"
FAKE_VAR = "EVIDENCE_FAKE"  # <PREFIJO>EVIDENCE_FAKE, solo con <PREFIJO>TESTING=1 (el de ci seal)


def _local(ctx: OpContext, value: str) -> Path:
    """Ruta de un argumento: absoluta o relativa al directorio actual."""
    path = Path(value)
    return path if path.is_absolute() else Path(ctx.inv.cwd) / path


def _input_error(*errors: Mapping[str, object]) -> OpError:
    return OpError("E_VALIDATION_INPUT", details={"errors": [dict(e) for e in errors]})


def _repository(directory: Path, env: Mapping[str, str]) -> Git:
    """Git de ``--repo``: un repositorio existente, sin red."""
    try:
        git = Git(directory, env=env, offline=True)
    except GitUnavailable as exc:
        raise OpError("E_CONFIG_GIT_UNAVAILABLE") from exc
    try:
        proc = git.run("rev-parse", "--git-dir", check=False)
    except (GitError, GitTimeout, GitUnavailable):
        proc = None
    if proc is None or proc.returncode != 0:
        raise _input_error({"param": "repo", "reason": "not_a_repository"})
    return git


def _fakes(env: Mapping[str, str]) -> tuple[Catalog | None, Any]:
    """``(catálogo, transporte)``: los reales (``None``) o, en las pruebas, los del archivo de
    ``<PREFIJO>EVIDENCE_FAKE``, con el mismo contrato que en ``ci seal``."""
    path = env_flag(env, FAKE_VAR) if testing(env) else None
    if not path:
        return None, None
    spec = importlib.util.spec_from_file_location(f"_{_meta.NAME}_evidence_fake", path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    catalog = module.catalog() if hasattr(module, "catalog") else None
    transport = module.transport() if hasattr(module, "transport") else None
    return catalog, transport


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _schema_pointers(kind: str, instance: object) -> list[str]:
    """Punteros JSON de los errores de ``instance`` contra ``schemas/<kind>``, sin los valores
    (un mensaje de jsonschema podría citar una sal)."""
    from ..config.schemas import validator

    errors = sorted(validator(kind).iter_errors(instance), key=lambda e: [str(p) for p in e.absolute_path])  # type: ignore[arg-type]
    return ["".join("/" + _escape(str(p)) for p in e.absolute_path) or "/" for e in errors]


# -- salida --------------------------------------------------------------------------------


def detail(check: ev.CheckResult, t: Translator) -> str:
    """Texto localizado de un check, de 300 caracteres o menos."""
    params: dict[str, object] = dict(check.params)
    params.update({name: t(key) for name, key in check.refs.items()})
    text = t(ev.detail_key(check.check, check.reason), **params)
    for key, extra in check.notes:
        text += " " + t(key, **dict(extra))
    return clip(text, MAX_DETAIL)


def _check_data(check: ev.CheckResult, t: Translator, zone: str) -> dict[str, object]:
    times = timefmt.times(check.time_utc, zone) if check.time_utc is not None else None
    return {
        "check": check.check,
        "source": check.source,
        "status": check.status,
        "time_utc": times.utc_text if times is not None else None,
        "time_local": times.local_text if times is not None else None,
        "reason": ev.reason_code(check),  # para las máquinas; detail es su texto localizado
        "detail": detail(check, t),
    }


def _record_data(result: ev.RecordResult, t: Translator, zone: str) -> dict[str, object]:
    return {
        "kind": result.kind,
        "commit": result.commit,
        "manifest_sha256": result.manifest_sha256,
        "verdict": result.verdict,
        "confidence": result.confidence,
        "checks": [_check_data(c, t, zone) for c in result.checks],
    }


def _without_block(results: Sequence[ev.RecordResult]) -> bool:
    """Queda alguna prueba OTS sin bloque, que los calendarios podrían completar."""
    return any(c.check == "ots" and c.reason == "pending" for r in results for c in r.checks)


def _warnings(results: Sequence[ev.RecordResult], t: Translator, *, no_network: bool) -> list[WarningObj]:
    """Avisos en el orden de 05 §12.2. ``W_NET_OFFLINE``: sin red quedó una prueba OTS sin
    bloque, o los calendarios consultados no respondieron."""
    out: list[WarningObj] = []
    if (no_network and _without_block(results)) or any(NET_OFFLINE in r.warnings for r in results):
        out.append(make_warning(NET_OFFLINE, t))
    if any(TOOL_MISSING in r.warnings for r in results):
        out.append(make_warning("W_CONFIG_TOOL_MISSING", t))
    if any(r.verdict == "verified" and r.confidence == "weak" for r in results):
        out.append(make_warning("W_VALIDATION_EVIDENCE_WEAK", t))
    return out


# -- operación -----------------------------------------------------------------------------


def _verify(folder: Path, **options: Any) -> ev.RecordResult:
    """Un registro, aislado de los demás: si no se puede identificar o sus archivos (de
    terceros) provocan un error inesperado, da ``failed`` con un motivo estable y la
    verificación sigue con las demás carpetas."""
    try:
        return ev.verify_folder(folder, **options)
    except ev.UnreadableRecord as exc:
        return ev.unverifiable(folder, exc.reason)
    except Exception as exc:  # noqa: BLE001 - p. ej., ValueError o RecursionError de un JSON hostil
        return ev.unverifiable(folder, error=type(exc).__name__)


def run(
    ctx: OpContext,
    *,
    path: str,
    repo: str | None = None,
    content: str | None = None,
    offline: bool = False,
    upgrade: bool = False,
    tz: str | None = None,
) -> Result:
    """``evidence verify <ruta> [--repo <dir>] [--content <dir>] [--offline] [--upgrade] [--tz <zona>]``."""
    errors: list[dict[str, object]] = []
    for name, value in (("path", path), ("repo", repo), ("content", content), ("tz", tz)):
        if (name == "path" or value is not None) and (not isinstance(value, str) or not value):
            errors.append({"param": name, "reason": "type"})
    for name, flag in (("offline", offline), ("upgrade", upgrade)):
        if not isinstance(flag, bool):
            errors.append({"param": name, "reason": "type"})
    if errors:
        raise _input_error(*errors)
    t = ctx.t()
    target = _local(ctx, path)
    dirs: dict[str, Path | None] = {"repo": None, "content": None}
    if not target.is_dir():
        errors.append({"param": "path", "reason": "not_a_directory" if target.exists() else "not_found"})
    for name, value in (("repo", repo), ("content", content)):
        if value is not None:
            dirs[name] = _local(ctx, value)
            if not dirs[name].is_dir():  # type: ignore[union-attr]
                errors.append({"param": name, "reason": "not_found"})
    zone = tz if tz is not None else timefmt.DEFAULT_TZ
    if tz is not None and timefmt.zone_status(tz) == "invalid":
        errors.append({"param": "tz", "reason": "unknown_zone"})
    if errors:
        raise _input_error(*errors)
    folders = ev.find_records(target)
    if not folders:
        raise _input_error({"param": "path", "reason": "no_records"})

    env = ctx.inv.env
    no_network = offline or env_flag(env, "OFFLINE") == "1"
    repo_git = _repository(dirs["repo"], env) if dirs["repo"] is not None else None
    catalog, transport = _fakes(env)
    calendars: ev.Upgrade | None = None  # sin red no se consultan (05 §9.26)
    if not no_network:
        mode: ev.UpgradeMode = "memory" if not upgrade else "dry_run" if ctx.inv.dry_run else "write"
        calendars = ev.Upgrade(mode=mode, transport=transport)  # un solo Budget para la corrida
    clean = ev.CleanForm(dirs["content"], env=env) if dirs["content"] is not None else None
    content_src = ev.Content(dirs["content"], clean) if dirs["content"] is not None else None
    results: list[ev.RecordResult] = []
    try:
        for folder in folders:
            results.append(
                _verify(
                    folder, catalog=catalog, repo=repo_git, content=content_src, upgrade=calendars,
                    validate=_schema_pointers,
                )
            )
    finally:
        if clean is not None:
            clean.close()

    final, confidence = ev.overall(results)
    data: dict[str, object] = {
        "target": path,
        "verdict": final,
        "confidence": confidence,
        "records": [_record_data(r, t, zone) for r in results],
    }
    warnings = _warnings(results, t, no_network=no_network)
    if final == "failed":
        raise OpError("E_VALIDATION_EVIDENCE_FAILED", data=data, warnings=warnings)
    return ok(OP, data, warnings)


# -- vista legible ---------------------------------------------------------------------------


def _verdict_text(t: Translator, verdict: object, confidence: object) -> str:
    text = t(f"evidence.verdict.{verdict}")
    if confidence:
        text += " " + t("evidence.render.confidence", confidence=t(f"evidence.confidence.{confidence}"))
    return text


def render_text(result: Result, t: Translator, *, fmt: str = "text") -> str:
    """Vista legible de ``data_evidence_verify``: el veredicto, cada registro y sus checks."""
    del fmt
    data = result.data or {}
    lines = [
        t(
            "evidence.render.header",
            title=t("evidence.verify"),
            target=data.get("target", ""),
            verdict=_verdict_text(t, data.get("verdict"), data.get("confidence")),
        )
    ]
    for record in data.get("records", []):
        lines.append(
            t(
                "evidence.render.record",
                kind=t(f"evidence.kind.{record.get('kind')}"),
                commit=record.get("commit", ""),
                verdict=_verdict_text(t, record.get("verdict"), record.get("confidence")),
            )
        )
        for item in record.get("checks", []):
            source = item.get("source")
            lines.append(
                t(
                    "evidence.render.check",
                    status=t(f"evidence.status.{item.get('status')}"),
                    check=t(f"evidence.check_name.{item.get('check')}"),
                    source=f" {source}" if source else "",
                    detail=item.get("detail") or "",
                )
            )
            if item.get("time_utc"):
                local = item.get("time_local")
                key = "evidence.render.time_local" if local else "evidence.render.time"
                lines.append(t(key, utc=item["time_utc"], local=local or ""))
    return "\n".join(lines)
