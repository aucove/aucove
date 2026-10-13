# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Verificación con red de las fijaciones de los workflows (10-seguridad REQ-SEC-47).

Uso (manual, con red; no corre en las pruebas unitarias)::

    python tools/verify_action_pins.py [--root DIR] [--list] [--skip-images] [--timeout S]

- Recorre ``.github/workflows/*.yml`` del framework y los workflows de ``template/``
  (``template/**/.github/workflows/*.yml``). Cada ``uses: dueño/repo@<sha> # <etiqueta>``
  DEBE tener un SHA de 40 caracteres y la etiqueta en el comentario; con
  ``git ls-remote`` se comprueba que la etiqueta existe en
  ``https://github.com/dueño/repo`` y que apunta a ese commit (se pela una etiqueta
  anotada con ``^{}``). Los workflows reutilizables locales (``./``) no se fijan.
- Lee el ``action.yml`` (o ``action.yaml``) de cada acción en el commit fijado. Si es una
  acción Docker (``runs.using: docker``), fijar su SHA no basta: su imagen DEBE estar
  fijada por digest, sea ``image: docker://...@sha256:`` o un Dockerfile cuyos ``FROM``
  lleven ``@sha256:``. Un ``FROM`` con una etiqueta mutable (``FROM fsfe/reuse:6``) es un
  error, y esas imágenes base se verifican en el registro como las demás. No se recorren
  los pasos de las acciones compuestas.
- Recorre las imágenes fijadas por digest: ``build.image`` de ``template/**/project.yaml``
  y, en los workflows, las ``image:``, los ``docker://`` y las variables ``*_IMAGE:`` (las
  imágenes que un paso ejecuta con ``docker run "$X_IMAGE"``). Con la API v2 del registro
  (``HEAD /v2/<repo>/manifests/sha256:...``, con el token anónimo que pide el propio
  registro) se comprueba que el digest existe. Si la etiqueta apunta hoy a otro
  digest, solo se avisa: lo normativo es el digest.
- ``--list`` no usa la red: lista lo encontrado y comprueba solo la forma.

Salida: 0 si todo está verificado; 1 ante cualquier fijación inválida, inexistente o
que no se pudo comprobar (falla cerrada).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GITHUB = "https://github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"
DOCKER_HUB = "registry-1.docker.io"
DEFAULT_TIMEOUT_S = 30.0
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)

_USES = re.compile(
    r"""^\s*(?:-\s+)?uses:\s*(?P<q>["']?)(?P<target>[^\s"'#]+)(?P=q)\s*(?:#\s*(?P<comment>.*?))?\s*$"""
)
# ``image:`` (container:, services:, project.yaml) o una variable ``*_IMAGE:`` de un env
_IMAGE = re.compile(
    r"""^\s*(?:-\s+)?(?:image|[A-Z][A-Z0-9_]*_IMAGE):\s*(?P<q>["']?)(?P<ref>[^\s"'#]+)(?P=q)\s*(?:#.*)?$"""
)
# claves del bloque runs: de un action.yml
_RUNS_KEY = re.compile(r"""^\s+(?P<key>using|image):\s*(?P<q>["']?)(?P<value>[^\s"'#]*)(?P=q)\s*(?:#.*)?$""")
_TOP_KEY = re.compile(r"^(?P<key>[A-Za-z0-9_-]+):")
_FROM = re.compile(r"^\s*FROM\s+(?P<rest>.*?)\s*$", re.IGNORECASE)
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")
_BEARER_PARAM = re.compile(r'(\w+)="([^"]*)"')

# (args de git ls-remote) -> stdout
LsRemote = Callable[[Sequence[str]], str]


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]  # claves en minúsculas
    body: bytes = b""


# (método, url, cabeceras) -> respuesta; nunca lanza por un estado HTTP
Http = Callable[[str, str, Mapping[str, str]], HttpResponse]


class NetError(Exception):
    """No se pudo consultar la red: la verificación falla cerrada."""


@dataclass(frozen=True, slots=True)
class Where:
    file: str  # ruta POSIX relativa a --root
    line: int

    def __str__(self) -> str:
        return f"{self.file}:{self.line}"


@dataclass(frozen=True, slots=True)
class Pin:
    action: str  # dueño/repo[/ruta]
    ref: str
    tag: str | None
    where: Where

    @property
    def repo(self) -> str:
        return "/".join(self.action.split("/")[:2])


@dataclass(frozen=True, slots=True)
class ImageRef:
    raw: str
    registry: str
    repository: str
    tag: str | None
    digest: str | None
    where: Where


@dataclass
class Outcome:
    ok: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def text(self) -> str:
        lines = [f"ok     {m}" for m in self.ok]
        lines += [f"aviso  {m}" for m in self.notes]
        lines += [f"ERROR  {m}" for m in self.errors]
        verdict = "todas las fijaciones verificadas" if not self.errors else f"{len(self.errors)} errores"
        lines.append(f"-- {len(self.ok)} verificadas, {len(self.notes)} avisos: {verdict}")
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Recolección (sin red)
# ---------------------------------------------------------------------------


def workflow_files(root: Path) -> list[Path]:
    """Workflows del framework y de la plantilla (``base`` y ``l10n/<lang>``)."""
    found: set[Path] = set()
    for pattern in (".github/workflows/*.yml", ".github/workflows/*.yaml",
                    "template/**/.github/workflows/*.yml", "template/**/.github/workflows/*.yaml"):
        found.update(p for p in root.glob(pattern) if p.is_file())
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


def image_files(root: Path) -> list[Path]:
    """Archivos con ``build.image``: los ``project.yaml`` de la plantilla."""
    return sorted((p for p in root.glob("template/**/project.yaml") if p.is_file()),
                  key=lambda p: p.relative_to(root).as_posix())


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")


def parse_image(raw: str, where: Where) -> ImageRef:
    """Separa ``[registro/]repo[:etiqueta][@sha256:...]`` (convención de Docker)."""
    ref = raw[len("docker://"):] if raw.startswith("docker://") else raw
    name, _, digest = ref.partition("@")
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, path = first, rest
    else:
        registry, path = DOCKER_HUB, name
        if "/" not in path:
            path = "library/" + path
    tag: str | None = None
    last = path.rsplit("/", 1)[-1]
    if ":" in last:
        path, _, tag = path.rpartition(":")
    return ImageRef(raw=raw, registry=registry, repository=path, tag=tag or None,
                    digest=digest or None, where=where)


def collect(root: Path) -> tuple[list[Pin], list[ImageRef]]:
    """Fijaciones de acciones e imágenes, en el orden de los archivos."""
    pins: list[Pin] = []
    images: list[ImageRef] = []
    for path in workflow_files(root):
        rel = path.relative_to(root).as_posix()
        for n, line in enumerate(_lines(path), 1):
            m = _USES.match(line)
            if m is not None:
                target = m.group("target")
                where = Where(rel, n)
                if target.startswith("./") or "${{" in target:
                    continue  # workflow reutilizable local: corre en el mismo commit
                if target.startswith("docker://"):
                    images.append(parse_image(target, where))
                    continue
                action, _, ref = target.partition("@")
                comment = (m.group("comment") or "").split()
                pins.append(Pin(action=action, ref=ref, tag=comment[0] if comment else None, where=where))
                continue
            im = _IMAGE.match(line)
            if im is not None and "${{" not in im.group("ref") and "{{" not in im.group("ref"):
                images.append(parse_image(im.group("ref"), Where(rel, n)))
    for path in image_files(root):
        rel = path.relative_to(root).as_posix()
        for n, line in enumerate(_lines(path), 1):
            im = _IMAGE.match(line)
            if im is not None and "{{" not in im.group("ref"):
                images.append(parse_image(im.group("ref"), Where(rel, n)))
    return pins, images


def pin_form_error(pin: Pin) -> str | None:
    """Error de forma de REQ-SEC-47 / REQ-CI-5, o ``None``."""
    if not _REPO.match(pin.repo):
        return f"{pin.where}: '{pin.action}' no es dueño/repo"
    if not _SHA40.match(pin.ref):
        return f"{pin.where}: {pin.action}@{pin.ref} no está fijada por un SHA de 40 caracteres"
    if pin.tag is None:
        return f"{pin.where}: {pin.action}@{pin.ref[:12]} no lleva la versión en un comentario"
    if not _TAG.match(pin.tag):
        return f"{pin.where}: la versión '{pin.tag}' del comentario no es una etiqueta válida"
    return None


def image_form_error(image: ImageRef) -> str | None:
    if image.digest is None:
        return f"{image.where}: la imagen {image.raw} no está fijada por @sha256:"
    if not _DIGEST.match(image.digest):
        return f"{image.where}: el digest de {image.raw} no es sha256 de 64 hex"
    return None


# ---------------------------------------------------------------------------
# Verificación con red (inyectable para las pruebas)
# ---------------------------------------------------------------------------


def default_ls_remote(timeout_s: float = DEFAULT_TIMEOUT_S) -> LsRemote:
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GCM_INTERACTIVE": "never"})

    def run(args: Sequence[str]) -> str:
        try:
            proc = subprocess.run(["git", "ls-remote", *args], env=env, capture_output=True,
                                  check=False, timeout=timeout_s)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise NetError(f"git ls-remote: {type(exc).__name__}") from exc
        if proc.returncode != 0:
            msg = " ".join(proc.stderr.decode("utf-8", "replace").split())[:200]
            raise NetError(f"git ls-remote salió con {proc.returncode}: {msg}")
        return proc.stdout.decode("utf-8", "replace")

    return run


def resolve_tag(ls_remote: LsRemote, repo: str, tag: str) -> str | None:
    """Commit al que apunta ``refs/tags/<tag>`` (pelada si es anotada); ``None`` si no existe."""
    ref = f"refs/tags/{tag}"
    out = ls_remote(["--tags", f"{GITHUB}/{repo}", ref, ref + "^{}"])
    direct: str | None = None
    peeled: str | None = None
    for line in out.splitlines():
        sha, _, name = line.strip().partition("\t")
        if name == ref + "^{}":
            peeled = sha.lower()
        elif name == ref:
            direct = sha.lower()
    return peeled or direct


def verify_pins(pins: Iterable[Pin], ls_remote: LsRemote, out: Outcome) -> None:
    groups: dict[tuple[str, str, str], list[Pin]] = {}
    for pin in pins:
        error = pin_form_error(pin)
        if error is not None:
            out.errors.append(error)
            continue
        assert pin.tag is not None
        groups.setdefault((pin.repo, pin.tag, pin.ref), []).append(pin)
    by_action: dict[str, set[str]] = {}
    for (repo, tag, sha), members in sorted(groups.items()):
        places = ", ".join(str(p.where) for p in members)
        for p in members:
            by_action.setdefault(p.action, set()).add(f"{tag} ({sha[:12]})")
        try:
            commit = resolve_tag(ls_remote, repo, tag)
        except NetError as exc:
            out.errors.append(f"{repo}@{sha[:12]} {tag}: no se pudo comprobar ({exc}) [{places}]")
            continue
        if commit is None:
            out.errors.append(f"{repo}: la etiqueta {tag} no existe [{places}]")
        elif commit != sha:
            out.errors.append(f"{repo}@{sha[:12]}: {tag} apunta a {commit[:12]}, no a ese SHA [{places}]")
        else:
            out.ok.append(f"{repo}@{sha[:12]} = {tag} [{places}]")
    for action, versions in sorted(by_action.items()):
        if len(versions) > 1:
            out.notes.append(f"{action} se fija con versiones distintas: {', '.join(sorted(versions))}")


# ---------------------------------------------------------------------------
# Imagen de las acciones Docker (REQ-SEC-47: fijar el SHA no fija un FROM mutable)
# ---------------------------------------------------------------------------


def _fetch_text(http: Http, url: str) -> str | None:
    """Texto de ``url``; ``None`` si no existe (404)."""
    resp = http("GET", url, {})
    if resp.status == 404:
        return None
    if resp.status != 200:
        raise NetError(f"GitHub respondió {resp.status}")
    try:
        return resp.body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NetError("archivo ilegible (no es UTF-8)") from exc


def parse_runs(text: str) -> tuple[str | None, str | None]:
    """``(runs.using, runs.image)`` de un ``action.yml``, sin dependencias de YAML."""
    using: str | None = None
    image: str | None = None
    in_runs = False
    for line in text.replace("\r\n", "\n").split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        top = _TOP_KEY.match(line)
        if top is not None:
            in_runs = top.group("key") == "runs"
            continue
        m = _RUNS_KEY.match(line) if in_runs else None
        if m is not None and m.group("key") == "using" and using is None:
            using = m.group("value").lower()
        elif m is not None and m.group("key") == "image" and image is None:
            image = m.group("value")
    return using, image


def dockerfile_bases(text: str) -> list[tuple[int, str]]:
    """``(línea, imagen)`` de cada ``FROM`` que no es ``scratch`` ni una etapa anterior."""
    stages: set[str] = set()
    bases: list[tuple[int, str]] = []
    for n, line in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        m = _FROM.match(line)
        if m is None:
            continue
        words = [w for w in m.group("rest").split() if not w.startswith("--")]
        image = words[0] if words else ""
        if image.lower() != "scratch" and image.lower() not in stages:
            bases.append((n, image))
        if len(words) >= 3 and words[1].lower() == "as":
            stages.add(words[2].lower())
    return bases


def _action_runtime(http: Http, pin: Pin, out: Outcome, places: str) -> list[ImageRef]:
    """Comprueba la imagen de una acción Docker; devuelve las imágenes base a verificar."""
    label = f"{pin.repo}@{pin.ref[:12]} {pin.tag}"
    sub = pin.action[len(pin.repo):].strip("/")
    prefix = f"{GITHUB_RAW}/{pin.repo}/{pin.ref}/" + (sub + "/" if sub else "")
    found: tuple[str, str] | None = None
    for name in ("action.yml", "action.yaml"):
        text = _fetch_text(http, prefix + name)
        if text is not None:
            found = (name, text)
            break
    if found is None:
        out.errors.append(f"{label}: no se encontró action.yml ni action.yaml en ese commit [{places}]")
        return []
    name, text = found
    meta_path = (sub + "/" if sub else "") + name
    using, image = parse_runs(text)
    if using is None:
        out.errors.append(f"{label}: su {meta_path} no declara runs.using [{places}]")
        return []
    if using != "docker":
        return []  # node*, composite: la imagen no aplica
    if not image:
        out.errors.append(f"{label}: acción Docker sin runs.image [{places}]")
        return []
    if image.startswith("docker://"):
        ref = parse_image(image, Where(f"{pin.repo}@{pin.ref[:12]}/{meta_path}", 1))
        if ref.digest is None or not _DIGEST.match(ref.digest):
            out.errors.append(
                f"{label}: es una acción Docker y su imagen {image} no está fijada por @sha256:; "
                f"fijar el SHA de la acción no fija la imagen [{places}]"
            )
            return []
        out.ok.append(f"{label}: acción Docker con la imagen fijada por digest [{places}]")
        return [ref]
    path = (sub + "/" if sub else "") + image.removeprefix("./")
    dockerfile = _fetch_text(http, f"{GITHUB_RAW}/{pin.repo}/{pin.ref}/{path}")
    if dockerfile is None:
        out.errors.append(f"{label}: acción Docker y no se encontró su {path} en ese commit [{places}]")
        return []
    bases = dockerfile_bases(dockerfile)
    if not bases:
        out.errors.append(f"{label}: el {path} de la acción Docker no tiene ningún FROM [{places}]")
        return []
    refs: list[ImageRef] = []
    bad = False
    for n, base in bases:
        ref = parse_image(base, Where(f"{pin.repo}@{pin.ref[:12]}/{path}", n))
        if "$" in base or ref.digest is None or not _DIGEST.match(ref.digest):
            bad = True
            why = "con una variable que no se puede verificar" if "$" in base else "sin @sha256:"
            out.errors.append(
                f"{label}: es una acción Docker y su {path} usa FROM {base}, {why}; "
                f"fijar el SHA de la acción no fija la imagen (REQ-SEC-47) [{places}]"
            )
        else:
            refs.append(ref)
    if bad:
        return []
    out.ok.append(f"{label}: acción Docker con su {path} fijado por digest [{places}]")
    return refs


def verify_action_runtimes(pins: Iterable[Pin], http: Http, out: Outcome) -> list[ImageRef]:
    """Lee el ``action.yml`` de cada acción fijada; falla cerrada si no se puede leer."""
    groups: dict[tuple[str, str], list[Pin]] = {}
    for pin in pins:
        if pin_form_error(pin) is None:
            groups.setdefault((pin.action, pin.ref), []).append(pin)
    images: list[ImageRef] = []
    for (action, sha), members in sorted(groups.items()):
        places = ", ".join(str(p.where) for p in members)
        try:
            images += _action_runtime(http, members[0], out, places)
        except NetError as exc:
            out.errors.append(f"{action}@{sha[:12]}: no se pudo leer su action.yml ({exc}) [{places}]")
    return images


def default_http(timeout_s: float = DEFAULT_TIMEOUT_S) -> Http:
    def run(method: str, url: str, headers: Mapping[str, str]) -> HttpResponse:
        if not url.startswith("https://"):
            raise NetError(f"solo https: {url}")
        req = urllib.request.Request(url, method=method, headers=dict(headers))
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 (https)
                body = resp.read(1 << 20) if method != "HEAD" else b""
                return HttpResponse(resp.status, {k.lower(): v for k, v in resp.headers.items()}, body)
        except urllib.error.HTTPError as exc:
            return HttpResponse(exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, b"")
        except (OSError, ValueError) as exc:
            raise NetError(f"{type(exc).__name__} en {urllib.parse.urlsplit(url).netloc}") from exc

    return run


def _bearer_token(http: Http, challenge: str, repository: str) -> str:
    """Token anónimo del desafío ``WWW-Authenticate: Bearer realm=...,service=...,scope=...``."""
    scheme, _, params_text = challenge.strip().partition(" ")
    if scheme.lower() != "bearer":
        raise NetError(f"autenticación del registro no admitida: {scheme or '?'}")
    params = dict(_BEARER_PARAM.findall(params_text))
    realm = params.get("realm", "")
    if not realm.startswith("https://"):
        raise NetError("el registro pide un token en una URL que no es https")
    query = {"service": params.get("service", ""), "scope": params.get("scope", f"repository:{repository}:pull")}
    sep = "&" if "?" in realm else "?"
    resp = http("GET", realm + sep + urllib.parse.urlencode({k: v for k, v in query.items() if v}), {})
    if resp.status != 200:
        raise NetError(f"el registro negó el token anónimo ({resp.status})")
    try:
        data = json.loads(resp.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NetError("respuesta de token ilegible") from exc
    token = (data.get("token") or data.get("access_token")) if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        raise NetError("respuesta de token sin token")
    return token


def manifest_head(http: Http, image: ImageRef, reference: str) -> HttpResponse:
    """``HEAD /v2/<repo>/manifests/<reference>``, con el token anónimo si el registro lo pide."""
    url = f"https://{image.registry}/v2/{image.repository}/manifests/{reference}"
    headers = {"Accept": MANIFEST_ACCEPT}
    resp = http("HEAD", url, headers)
    if resp.status == 401 and "www-authenticate" in resp.headers:
        token = _bearer_token(http, resp.headers["www-authenticate"], image.repository)
        resp = http("HEAD", url, {**headers, "Authorization": f"Bearer {token}"})
    return resp


def verify_images(images: Iterable[ImageRef], http: Http, out: Outcome) -> None:
    seen: set[str] = set()
    for image in images:
        error = image_form_error(image)
        if error is not None:
            out.errors.append(error)
            continue
        assert image.digest is not None
        key = f"{image.registry}/{image.repository}@{image.digest}"
        if key in seen:
            continue
        seen.add(key)
        label = f"{image.registry}/{image.repository}@{image.digest[:19]}"
        try:
            resp = manifest_head(http, image, image.digest)
        except NetError as exc:
            out.errors.append(f"{label}: no se pudo comprobar ({exc}) [{image.where}]")
            continue
        found = resp.headers.get("docker-content-digest")
        if resp.status == 404:
            out.errors.append(f"{label}: el digest no existe en el registro [{image.where}]")
            continue
        if resp.status != 200:
            out.errors.append(f"{label}: el registro respondió {resp.status} [{image.where}]")
            continue
        if found is not None and found.strip().lower() != image.digest:
            out.errors.append(f"{label}: el registro devolvió otro digest ({found[:19]}) [{image.where}]")
            continue
        out.ok.append(f"{label} existe [{image.where}]")
        if image.tag is not None:
            try:
                tag_resp = manifest_head(http, image, image.tag)
            except NetError:
                out.notes.append(f"{label}: no se pudo leer la etiqueta {image.tag}")
                continue
            tag_digest = tag_resp.headers.get("docker-content-digest")
            if tag_resp.status == 404:
                out.notes.append(f"{label}: la etiqueta {image.tag} ya no existe (rige el digest)")
            elif tag_resp.status == 200 and tag_digest and tag_digest.strip().lower() != image.digest:
                out.notes.append(f"{label}: la etiqueta {image.tag} apunta hoy a {tag_digest[:19]} (rige el digest)")


def run(
    root: Path,
    *,
    ls_remote: LsRemote | None = None,
    http: Http | None = None,
    skip_images: bool = False,
    list_only: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> tuple[int, str]:
    """``(código de salida, informe)``."""
    pins, images = collect(root)
    out = Outcome()
    if not pins:
        out.errors.append("no se encontró ningún uses: fijado en los workflows")
    if list_only:
        for pin in pins:
            error = pin_form_error(pin)
            if error is None:
                out.ok.append(f"{pin.action}@{pin.ref[:12]} {pin.tag} [{pin.where}]")
            else:
                out.errors.append(error)
        for image in images:
            error = image_form_error(image)
            if error is None:
                out.ok.append(f"{image.raw} [{image.where}]")
            else:
                out.errors.append(error)
        return (1 if out.errors else 0), out.text()
    verify_pins(pins, ls_remote or default_ls_remote(timeout_s), out)
    net = http or default_http(timeout_s)
    action_images = verify_action_runtimes(pins, net, out)
    if not skip_images:
        if not any(i.where.file.startswith("template/") and i.where.file.endswith("project.yaml") for i in images):
            out.errors.append("no se encontró la imagen TeX (build.image) en template/")
        verify_images([*images, *action_images], net, out)
    return (1 if out.errors else 0), out.text()


def main(
    argv: Sequence[str] | None = None, *, ls_remote: LsRemote | None = None, http: Http | None = None
) -> int:
    parser = argparse.ArgumentParser(
        prog="verify_action_pins.py",
        description=(
            "Comprueba con la red los SHA de las acciones, la imagen de las acciones Docker y los "
            "digests de las imágenes (REQ-SEC-47)."
        ),
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="raíz del repo del framework")
    parser.add_argument("--list", action="store_true", help="solo lista y comprueba la forma, sin red")
    parser.add_argument("--skip-images", action="store_true", help="no consulta el registro de imágenes")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="segundos por consulta")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # error de uso: 1, como el resto de tools/
        return 0 if exc.code == 0 else 1
    code, text = run(args.root.resolve(), ls_remote=ls_remote, http=http, skip_images=args.skip_images,
                     list_only=args.list, timeout_s=args.timeout)
    stream = sys.stdout if code == 0 else sys.stderr
    stream.write(text)
    stream.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
