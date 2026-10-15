# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Kit del árbitro que ``ci seal`` copia en cada carpeta de registro (11-trazabilidad §8.6, REQ-TRACE-82).

- ``VERIFY.md``, en el idioma de la política, en lenguaje llano: qué prueba y qué no, un solo
  comando con la versión que selló, OpenSSL con el ``-attime`` de cada sello y las huellas
  de su raíz, OpenTimestamps y las fórmulas de los compromisos.
- ``open_commitments.py``: 40 líneas o menos, solo la biblioteca estándar de Python 3.8 y sin
  el núcleo. Se ejecuta en un subproceso con ``-I -S`` (sin ``site``, así que el núcleo
  instalado no está en ``sys.path``) sobre la carpeta de un registro sellado.
"""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("rfc3161_client", reason="falta el extra evidence (el CI lo instala)")
pytest.importorskip("opentimestamps", reason="falta el extra evidence (el CI lo instala)")

from aucove import _meta  # noqa: E402
from aucove.evidence.record import OPEN_SCRIPT, VERIFY_FILE, read_record_dir  # noqa: E402
from aucove.ops import ci_seal  # noqa: E402
from tests.fixtures.evidence.tsa import fake_tsa as ft  # noqa: E402
from tests.unit.evidence.support import needs_git  # noqa: E402
from tests.unit.test_ci_seal import (  # noqa: E402
    BASE_FILES,
    DATA_CSV,
    METHODS_V2,
    Services,
    World,
    e1,
)

pytestmark = needs_git

SCRIPT = ci_seal.EVIDENCE_DATA / OPEN_SCRIPT
TEMPLATES = {lang: ci_seal.EVIDENCE_DATA / f"VERIFY.{lang}.md" for lang in ("es", "en")}
STDLIB = frozenset({"hashlib", "json", "sys", "pathlib"})


@pytest.fixture(scope="module")
def pkis() -> tuple[ft.Pki, ft.Pki]:
    return ft.make_pki("alpha"), ft.make_pki("beta")


def _sealed(tmp: Path, pkis: tuple[ft.Pki, ft.Pki], *, lang: str = "es") -> tuple[World, Path, Path]:
    """Carpetas de los registros de integración (con TSA) y de entrega (sin TSA) de E1."""
    world = World(tmp, Services.make(pkis), lang=lang)
    shas = world.build(e1)
    world.forge.seed_pull_request(files=[], number=5, head_branch="task/3", head_sha=shas["h2"], author="carla",
                                  merged=True, merge_commit_sha=shas["s1"], merged_by="ana")
    world.push(shas["c0"])
    integration = world.push(shas["s1"])
    submission = world.seal("submission", shas["h2"], pr=5, base_sha=shas["c0"])
    assert integration.rc == 0 and submission.rc == 0
    return world, integration.folder(), submission.folder()


@pytest.fixture(scope="module")
def sealed(tmp_path_factory: pytest.TempPathFactory, pkis: tuple[ft.Pki, ft.Pki]) -> tuple[World, Path, Path]:
    return _sealed(tmp_path_factory.mktemp("kit"), pkis)


# -- open_commitments.py ------------------------------------------------------------------------


def test_script_is_short_python_38_and_stdlib_only() -> None:
    data = SCRIPT.read_bytes()
    assert data.endswith(b"\n") and b"\r" not in data and not data.startswith(b"\xef\xbb\xbf")
    assert data.count(b"\n") <= 40  # REQ-TRACE-82
    tree = ast.parse(data.decode("utf-8"), feature_version=(3, 8))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "sin imports relativos"
            imported.add((node.module or "").split(".")[0])
    assert imported == STDLIB  # sin el núcleo ni dependencias
    assert _meta.NAME not in data.decode("utf-8").split('"""')[-1]  # el código no nombra el núcleo


def _run_script(folder: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    # -I aísla del entorno y -S quita site: ni PYTHONPATH ni el núcleo instalado llegan al script.
    return subprocess.run(
        [sys.executable, "-I", "-S", OPEN_SCRIPT, "manifest.json", "openings.json", *extra],
        cwd=folder, capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
    )


def _checkout(tmp: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        target = tmp.joinpath(*rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return tmp


def test_script_opens_the_commitments_of_a_real_record_without_the_core(
    sealed: tuple[World, Path, Path], tmp_path: Path
) -> None:
    _world, folder, _submission = sealed
    assert (folder / OPEN_SCRIPT).read_bytes() == SCRIPT.read_bytes()
    plain = _run_script(folder)
    assert plain.returncode == 0, plain.stdout + plain.stderr
    lines = plain.stdout.splitlines()
    assert lines[0].split() == ["ok", "manifest.json"]
    assert {line.split()[1] for line in lines[1:]} == {
        "sections/methods/data.csv", "sections/methods/methods.tex",
        "actor:pr_author", "actor:merged_by", "actor:author", "actor:committer",
    }
    assert all(line.startswith("ok ") for line in lines)
    files = {"sections/methods/methods.tex": METHODS_V2, "sections/methods/data.csv": DATA_CSV}
    good = _run_script(folder, str(_checkout(tmp_path / "good", files)))
    assert good.returncode == 0, good.stdout
    bad = _run_script(folder, str(_checkout(tmp_path / "bad", {"sections/methods/methods.tex": b"otra cosa\n"})))
    assert bad.returncode == 1
    states = dict(reversed(line.split()) for line in bad.stdout.splitlines())
    assert states["sections/methods/methods.tex"] == "different"
    assert states["sections/methods/data.csv"] == "missing"


def test_script_detects_openings_of_another_manifest(sealed: tuple[World, Path, Path], tmp_path: Path) -> None:
    _world, folder, submission = sealed
    mixed = tmp_path / "mixed"
    shutil.copytree(folder, mixed)
    shutil.copyfile(submission / "openings.json", mixed / "openings.json")
    got = _run_script(mixed)
    assert got.returncode == 1
    assert got.stdout.splitlines()[0].split() == ["different", "manifest.json"]


def _commitment(domain: bytes, salt: str, data: bytes) -> str:
    return hashlib.sha256(domain + b"\0" + bytes.fromhex(salt) + data).hexdigest()


def _kit(folder: Path, sealed: dict[str, bytes], opened: list[str], salt: str) -> None:
    """``manifest.json`` con las entradas ``sealed`` (ruta -> contenido) y ``openings.json``
    con las aperturas de ``opened``, todas con la misma sal y sin actores."""
    entries = [{"p": _commitment(b"evidence.path.v1", salt, p.encode()), "c": _commitment(b"evidence.blob.v1", salt, c)}
               for p, c in sealed.items()]
    raw = json.dumps({"entries": entries, "actors": []}, separators=(",", ":")).encode()
    (folder / "manifest.json").write_bytes(raw)
    openings = {"manifest_sha256": hashlib.sha256(raw).hexdigest(), "actors": [],
                "entries": [{"path": path, "salt": salt} for path in opened]}
    (folder / "openings.json").write_text(json.dumps(openings), encoding="utf-8")


def test_script_escapes_what_cannot_be_printed(tmp_path: Path) -> None:
    """Una apertura con secuencias de terminal en la ruta no borra ni tapa la línea
    del archivo distinto: sale escapada y en su propia línea."""
    shutil.copyfile(SCRIPT, tmp_path / OPEN_SCRIPT)
    spoof = "\r\x1b[2K\x1b[1A\x1b[2K" + "%-16s%s" % ("ok", "paper.tex")  # borra esta línea y la anterior
    _kit(tmp_path, {"paper.tex": b"version sellada\n"}, ["paper.tex", spoof, "nota‮txt.md"], "11" * 32)
    files = tmp_path / "files"
    files.mkdir()
    (files / "paper.tex").write_bytes(b"version distinta, escrita despues\n")
    got = _run_script(tmp_path, "files")
    assert got.returncode == 1
    assert got.stdout.splitlines() == [
        "ok              manifest.json",
        "different       paper.tex",
        "not-in-manifest \\r\\x1b[2K\\x1b[1A\\x1b[2Kok              paper.tex",
        "not-in-manifest nota\\u202etxt.md",
    ]
    assert "\x1b" not in got.stdout and "‮" not in got.stdout


def test_script_never_reads_absolute_paths_or_paths_with_dotdot(tmp_path: Path) -> None:
    """Una ruta absoluta o con ``..`` da ``different`` y no se lee, aunque el archivo de
    afuera tenga justo lo sellado (antes, el programa lo leía y decía ``ok``)."""
    shutil.copyfile(SCRIPT, tmp_path / OPEN_SCRIPT)
    secret = tmp_path / "fuera.txt"
    secret.write_bytes(b"secreto\n")
    paths = ["../fuera.txt", secret.as_posix(), "sub/../../fuera.txt"]
    _kit(tmp_path, {p: b"secreto\n" for p in paths}, paths, "22" * 32)
    (tmp_path / "files").mkdir()
    for extra in (("files",), ()):  # con la carpeta y sin ella
        got = _run_script(tmp_path, *extra)
        assert got.returncode == 1
        assert got.stdout.splitlines()[1:] == ["%-16s%s" % ("different", p) for p in paths]


# -- VERIFY.md -------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["es", "en"])
def test_templates_are_plain_markdown_with_known_markers(lang: str) -> None:
    text = TEMPLATES[lang].read_bytes().decode("utf-8")
    assert "\r" not in text and text.endswith("\n") and not text.endswith("\n\n")
    assert set(ci_seal._PLACEHOLDER_RE.findall(text)) == {
        "name", "version", "kind", "commit", "attempt", "artifact", "manifest_sha256", "tsa_table", "tsa_commands",
        # la firma del proyecto (11-trazabilidad §8.6 y §8.10; 00-marco D-72)
        "sealed", "namespace", "key_id", "key", "verify_time", "open_section",
    }
    assert sorted(m["name"] for m in ci_seal._BLOCK_RE.finditer(text)) == [
        "no-signature", "no-tsa", "signature", "signature", "signature", "signature", "signature", "tsa",
    ]
    assert "fecha cierta" not in text.lower() and "certain date" not in text.lower()  # REQ-TRACE-75
    assert "sección" not in text.lower()


def test_both_languages_cover_the_same_commands() -> None:
    es, en = (TEMPLATES[lang].read_text(encoding="utf-8") for lang in ("es", "en"))
    assert ci_seal._BLOCK_RE.findall(es) and len(es.splitlines()) == len(en.splitlines())
    for needle in ("```sh", "ots --no-bitcoin verify {{sealed}}.ots", "ots info {{sealed}}.ots",
                   "python open_commitments.py manifest.json openings.json", "`Verification: OK`",
                   'uvx --from "{{name}}[evidence]=={{version}}" {{name}} evidence verify .',
                   "evidence verify . --keys",
                   "ssh-keygen -Y verify -f allowed_signers -I {{key_id}} -n {{namespace}} -s manifest.json.sig",
                   "-O verify-time={{verify_time}} < manifest.json",
                   "ssh-keygen -Y check-novalidate -n {{namespace}} -s manifest.json.sig < manifest.json"):
        assert es.count(needle) == en.count(needle) >= 1, needle


def test_verify_md_of_an_integration_record(sealed: tuple[World, Path, Path]) -> None:
    world, folder, _submission = sealed
    text = (folder / VERIFY_FILE).read_text(encoding="utf-8")
    record = read_record_dir(folder).record
    assert "{{" not in text and "<!--" not in text
    assert text.startswith("# Cómo verificar este registro de evidencia\n")
    assert "**prueba de existencia en una fecha**" in text
    assert f'uvx --from "{_meta.NAME}[evidence]=={_meta.__version__}" {_meta.NAME} evidence verify .' in text
    assert record.manifest_sha256 in text and record.artifact in text and record.commit in text
    attime = int(ft.GEN_TIME.timestamp())
    for pki in world.services.pkis:
        root = (folder / "tsa" / f"{pki.name}.root.pem").read_bytes()
        assert hashlib.sha256(root).hexdigest() in text
        der = ft.der(pki.root)
        assert ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest()) in text
        assert (
            f"openssl ts -verify -data manifest.json -in tsa/{pki.name}.tsr -CAfile tsa/{pki.name}.root.pem"
            f" -untrusted tsa/{pki.name}.chain.pem -attime {attime}"
        ) in text
        assert f"<https://tsa.{pki.name}.test/root>" in text
    assert "Este registro no trae sellos RFC 3161" not in text


def test_verify_md_of_a_submission_has_no_tsa_section(sealed: tuple[World, Path, Path]) -> None:
    _world, _integration, folder = sealed
    text = (folder / VERIFY_FILE).read_text(encoding="utf-8")
    assert "Este registro no trae sellos RFC 3161" in text
    assert "openssl ts -verify" not in text and "{{" not in text and "<!--" not in text
    assert "registro `submission`" in text


def test_verify_md_follows_the_language_of_the_policy(tmp_path: Path, pkis: tuple[ft.Pki, ft.Pki]) -> None:
    world = World(tmp_path, Services.make(pkis), lang="en")
    shas = world.build(lambda s: {"c0": s.commit("main", dict(BASE_FILES), message="init")})
    text = (world.seal("integration", shas["c0"]).folder() / VERIFY_FILE).read_text(encoding="utf-8")
    assert text.startswith("# How to verify this evidence record\n")
    assert "**proof of existence at a date**" in text and "{{" not in text


def test_render_verify_without_catalog_uses_the_ids(sealed: tuple[World, Path, Path]) -> None:
    _world, folder, _submission = sealed
    record = read_record_dir(folder).record
    text = ci_seal.render_verify(record, "en", None)
    assert "| alpha (`alpha`) |" in text and "# alpha (alpha)" in text


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl no está instalado")
def test_the_openssl_command_of_verify_md_accepts_the_stamp(sealed: tuple[World, Path, Path]) -> None:
    """El comando de ``VERIFY.md`` responde ``Verification: OK`` con los archivos de la carpeta."""
    _world, folder, _submission = sealed
    text = (folder / VERIFY_FILE).read_text(encoding="utf-8")
    commands = [line for line in text.splitlines() if line.startswith("openssl ts -verify")]
    assert len(commands) == 2
    openssl = shutil.which("openssl")
    assert openssl is not None
    for command in commands:
        proc = subprocess.run([openssl, *command.split()[1:]], cwd=folder, capture_output=True, timeout=60,
                              check=False)
        assert proc.returncode == 0 and b"Verification: OK" in proc.stdout, proc.stdout + proc.stderr
    # el mismo sello no vale para otros bytes
    tampered = folder.parent / "tampered"
    shutil.copytree(folder, tampered)
    (tampered / "manifest.json").write_bytes((folder / "manifest.json").read_bytes() + b" ")
    proc = subprocess.run([openssl, *commands[0].split()[1:]], cwd=tampered, capture_output=True, timeout=60,
                          check=False)
    assert b"Verification: OK" not in proc.stdout
