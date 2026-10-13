# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Las licencias que deja ``init`` (11-trazabilidad §7.3, REQ-TRACE-61 a 64; 00-marco D-64).

Sobre el mismo mundo que ``test_op_init``: un repo recién creado desde la plantilla publicada y
el falso. La persona responde en la terminal (``Invocation.ask``); sin respuestas, ``init`` usa
las sugerencias de la tabla según la visibilidad del repositorio. Si ``reuse`` está instalado
(``.venv-tools``, el ``PATH`` o ``AUCOVE_REUSE``), el árbol que deja ``init`` pasa ``reuse lint``.
"""

from __future__ import annotations

import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from aucove import _meta
from aucove.config.datafiles import license_text
from aucove.config.project import parse_project
from aucove.domain.licenses import ALL_RIGHTS_RESERVED, TEMPLATE_HOLDER
from aucove.i18n import translator
from aucove.ops.result import Result
from tests.board_world import NOW, Typed, World
from tests.render.test_template_content import find_reuse
from tests.unit.test_op_init import LEAD, Terminal, init_world, valid

SPDX = "SPDX" + "-"  # por concatenación: reuse no la lee como etiqueta de este archivo
ES = translator("es")


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    from aucove.ops import init as init_op

    monkeypatch.setattr(init_op, "_sleep", lambda seconds: None)


@dataclass
class Answers:
    """Las respuestas a las preguntas de ``init``, en orden; sin respuesta, la línea vacía."""

    replies: list[str]
    prompts: list[str] = field(default_factory=list)

    def __call__(self, prompt: str, default: str) -> str:
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else ""


def run(w: World, answers: Answers | None = None, *, typed: Typed | None = None, dry_run: bool = False,
        **kwargs: object) -> Result:
    term = Terminal()
    inv: dict[str, object] = {"surface": "cli", "tty": True, "confirm": typed or Typed(),
                              "phrase": term.phrase, "secret": term.secret}
    if answers is not None:
        inv["ask"] = answers
    return w.run("init", login=LEAD, lang="es", dry_run=dry_run, inv=inv, **kwargs)


def licenses_dir(w: World) -> list[str]:
    return sorted(p.name for p in (w.repo.path / "LICENSES").iterdir())


def annotations(w: World) -> list[tuple[object, object, object]]:
    data = tomllib.loads((w.repo.path / "REUSE.toml").read_text(encoding="utf-8"))
    return [(a["path"], a[SPDX + "FileCopyrightText"], a[SPDX + "License-Identifier"]) for a in data["annotations"]]


def reuse_lint(w: World) -> None:
    """REUSE 3.3 sobre el árbol que dejó ``init`` (D-64), si ``reuse`` está instalado."""
    reuse = find_reuse()
    if reuse is None:
        return
    proc = subprocess.run([reuse, "--root", str(w.repo.path), "lint"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def files_of(result: Result) -> dict[str, str]:
    return {f["path"]: f["action"] for f in result.data["files"]}  # type: ignore[index]


def test_the_answers_go_to_project_yaml_license_and_reuse_toml(tmp_path: Path) -> None:
    w = init_world(tmp_path)
    answers = Answers(["Lab X «UNAM»", "CC-BY-SA-4.0", "GPL-3.0-or-later", "ODbL-1.0"])
    typed = Typed()
    result = valid(run(w, answers, typed=typed, language="es"))
    assert result.ok, result.error
    assert len(answers.prompts) == 4 and "[Los autores de estudio]" in answers.prompts[0]
    assert "[CC-BY-4.0]" in answers.prompts[1] and "[MIT]" in answers.prompts[2] and "[CC0-1.0]" in answers.prompts[3]
    # project.yaml y LICENSES/: exactamente los textos en uso (bib/ es data), los del paquete
    cfg, _ = parse_project((w.repo.path / "project.yaml").read_bytes())
    assert cfg is not None and dict(cfg.licenses or {}) == {
        "holder": "Lab X «UNAM»", "text": "CC-BY-SA-4.0", "code": "GPL-3.0-or-later", "data": "ODbL-1.0"}
    assert licenses_dir(w) == ["CC-BY-SA-4.0.txt", "GPL-3.0-or-later.txt", "ODbL-1.0.txt"]
    for name in licenses_dir(w):
        assert (w.repo.path / "LICENSES" / name).read_bytes() == license_text(name[:-4])
    files = files_of(result)
    assert files["LICENSES/CC0-1.0.txt"] == files["LICENSES/MIT-0.txt"] == "deleted"
    # REUSE.toml: text, code, el código de la plantilla con los dos titulares, data y los skills
    owner = f"{NOW.year} Lab X «UNAM»"
    rows = annotations(w)
    assert [r[2] for r in rows] == ["CC-BY-SA-4.0", "GPL-3.0-or-later", "GPL-3.0-or-later", "ODbL-1.0", "MIT-0"]
    assert rows[0][:2] == (["**"], owner) and rows[2][1] == [owner, TEMPLATE_HOLDER]
    template_code = rows[2][0]
    assert isinstance(template_code, list)
    assert {".github/workflows/build.yml", ".github/CODEOWNERS", "tex/main.tex", ".gitignore"} <= set(template_code)
    assert "plan.md" not in template_code and rows[4][1] == TEMPLATE_HOLDER
    # LICENSE: el resumen de las tres licencias, en el idioma del proyecto
    summary = (w.repo.path / "LICENSE").read_text(encoding="utf-8")
    assert summary.startswith("Licencias de estudio\n")
    assert ES("init.license.file.generated", name=_meta.NAME) in summary.split("\n")
    assert "CC-BY-SA-4.0, en LICENSES/CC-BY-SA-4.0.txt" in summary and "Lab X «UNAM»" in summary
    # La fase 2 muestra las licencias y, con una licencia abierta para text, el aviso de REQ-TRACE-63
    plan = typed.seen[0][2]
    assert ES("init.license.open_text", id="CC-BY-SA-4.0") in plan and "texto CC-BY-SA-4.0" in plan
    reuse_lint(w)


def test_a_second_run_keeps_everything_and_does_not_ask(tmp_path: Path) -> None:
    w = init_world(tmp_path)
    assert run(w, Answers([]), language="es").ok
    answers = Answers([])
    typed = Typed()
    again = valid(run(w, answers, typed=typed))
    assert again.ok and again.data["files"] == [] and answers.prompts == []  # type: ignore[index]
    assert ES("init.license.open_text", id="CC-BY-4.0") not in typed.seen[0][2]  # ya estaba elegida


def test_a_private_repository_reserves_all_rights(tmp_path: Path) -> None:
    w = init_world(tmp_path, visibility="private")
    result = valid(run(w, language="es"))  # sin respuestas: las sugerencias de un repo privado
    assert result.ok, result.error
    assert licenses_dir(w) == [f"{ALL_RIGHTS_RESERVED}.txt", "MIT.txt"]
    text = (w.repo.path / "LICENSES" / f"{ALL_RIGHTS_RESERVED}.txt").read_text(encoding="utf-8")
    assert text == ES("init.license.file.all_rights_reserved", year=NOW.year, holder="Los autores de estudio")
    assert [r[2] for r in annotations(w)] == [ALL_RIGHTS_RESERVED, "MIT", "MIT", ALL_RIGHTS_RESERVED, "MIT-0"]
    reuse_lint(w)
    assert valid(run(w)).data["files"] == []  # type: ignore[index]


def test_format_none_leaves_out_the_unused_data_license(tmp_path: Path) -> None:
    """D-64: sin archivos de datos, LICENSES/ no lleva CC0-1.0 y LICENSE dice que irá ahí."""
    w = init_world(tmp_path)
    result = valid(run(w, language="en", format="none"))
    assert result.ok, result.error
    assert licenses_dir(w) == ["CC-BY-4.0.txt", "MIT.txt"]
    summary = (w.repo.path / "LICENSE").read_text(encoding="utf-8")
    assert "There are no files of this category yet; its text will go in LICENSES/CC0-1.0.txt" in summary
    reuse_lint(w)
    found = w.run("validate", login=LEAD, lang="en", checks=["convention"])
    codes = {f["code"] for r in found.data["results"] for f in r["findings"]}  # type: ignore[index, union-attr]
    assert "E_VALIDATION_LICENSE_UNKNOWN" not in codes


def test_an_invalid_answer_is_asked_again(tmp_path: Path) -> None:
    w = init_world(tmp_path)
    answers = Answers(["", "GPL-3.0", "CC0-1.0"])  # GPL-3.0 es un identificador obsoleto de SPDX
    result = valid(run(w, answers, language="es"))
    assert result.ok, result.error
    assert answers.prompts[2].startswith(ES("init.license.invalid", value="GPL-3.0", version="3.27.0"))
    cfg, _ = parse_project((w.repo.path / "project.yaml").read_bytes())
    assert cfg is not None and dict(cfg.licenses or {})["text"] == "CC0-1.0"


def test_three_invalid_answers_write_nothing(tmp_path: Path) -> None:
    w = init_world(tmp_path)
    result = run(w, Answers(["", "x y", "Nope-1", "MIT+"]), language="es")
    assert result.error is not None and result.error.code == "E_VALIDATION_INPUT"
    assert result.error.details["errors"][0]["param"] == "licenses.text"  # type: ignore[index]
    assert w.forge.calls.writes() == [] and w.repo.git("status", "--porcelain").strip() == ""


def test_a_license_without_packaged_text_asks_for_its_file(tmp_path: Path) -> None:
    """D-64: el paquete no trae LGPL-3.0-or-later; la persona da la ruta de su texto."""
    w = init_world(tmp_path)
    text = tmp_path / "lgpl.txt"
    text.write_bytes(b"\xef\xbb\xbfGNU LESSER GENERAL PUBLIC LICENSE\r\nVersion 3\r\n")
    answers = Answers(["", "", "LGPL-3.0-or-later", "", str(tmp_path / "missing.txt"), str(text)])
    result = valid(run(w, answers, language="es"))
    assert result.ok, result.error
    assert answers.prompts[5].startswith(ES("init.license.text_bad", path=str(tmp_path / "missing.txt")))
    written = (w.repo.path / "LICENSES" / "LGPL-3.0-or-later.txt").read_bytes()
    assert written == b"GNU LESSER GENERAL PUBLIC LICENSE\nVersion 3\n"
    reuse_lint(w)


def test_without_the_text_init_writes_nothing(tmp_path: Path) -> None:
    w = init_world(tmp_path)
    result = run(w, Answers(["", "", "LGPL-3.0-or-later", "", ""]), language="es")
    assert result.error is not None and result.error.code == "E_VALIDATION_INPUT"
    assert result.error.details["errors"][0]["reason"] == "text_missing"  # type: ignore[index]
    assert w.forge.calls.writes() == [] and w.repo.git("status", "--porcelain").strip() == ""


def test_changed_licenses_are_regenerated_and_custom_files_are_kept(tmp_path: Path) -> None:
    w = init_world(tmp_path)
    assert run(w, language="es").ok  # público: CC-BY-4.0, MIT y CC0-1.0
    project = (w.repo.path / "project.yaml").read_text(encoding="utf-8")
    edited = project.replace("  text: CC-BY-4.0", "  text: CC0-1.0").replace("  code: MIT", "  code: Apache-2.0")
    assert edited != project
    w.repo.commit("licencias nuevas", files={"project.yaml": edited, "LICENSES/LicenseRef-Mia.txt": "propia\n"})
    result = valid(run(w))  # la base ya está protegida: los archivos quedan en el árbol de trabajo
    assert result.ok, result.error
    files = files_of(result)
    assert {"LICENSES/Apache-2.0.txt", "LICENSES/CC-BY-4.0.txt", "LICENSES/MIT.txt", "LICENSE", "REUSE.toml"} <= set(files)
    assert licenses_dir(w) == ["Apache-2.0.txt", "CC0-1.0.txt", "LicenseRef-Mia.txt"]
    assert [r[2] for r in annotations(w)][:2] == ["CC0-1.0", "Apache-2.0"]  # los generó init: se regeneran
    assert "LICENSES/LicenseRef-Mia.txt" not in files
    pending = result.data["pending_steps"]  # type: ignore[index]
    assert ES("init.pending.license_unused", path="LICENSES/LicenseRef-Mia.txt") in pending  # type: ignore[operator]
    # Un LICENSE editado a mano ya no es el de init: se deja y se avisa
    w.repo.git("checkout", "-q", "--", ".")
    w.repo.git("clean", "-q", "-fd")
    w.repo.commit("mi LICENSE", files={"LICENSE": "Todo es mío.\n"})
    kept = valid(run(w))
    assert kept.ok and "LICENSE" not in files_of(kept)
    assert ES("init.pending.modified", path="LICENSE") in kept.data["pending_steps"]  # type: ignore[index, operator]


def test_dry_run_does_not_ask(tmp_path: Path) -> None:
    w = init_world(tmp_path)
    answers = Answers(["Otra"])
    result = valid(run(w, answers, dry_run=True, language="es"))
    assert result.ok and answers.prompts == []
    effects = {e["target"] for e in result.data["plan"]}  # type: ignore[index]
    assert {"LICENSE", "REUSE.toml", "LICENSES/CC-BY-4.0.txt", "LICENSES/MIT.txt"} <= effects
