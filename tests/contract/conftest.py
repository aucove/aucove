# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Suite de contrato T2 del adaptador de la forja (08-forja §10.2; 12-mvp §3.3).

La fixture ``forge`` se parametriza con ``fake`` y ``github`` (REQ-MVP-26). El
caso ``github`` lleva el marcador ``live`` y usa ``GitHubAdapter`` contra los repos
sandbox de 08 §10.2: ``<PREFIJO>CONTRACT_REPO`` (p. ej. ``proyecto-test-13/ct-public``)
y los tokens de las cuentas del sandbox en ``TEST_GH_TOKEN_LEAD``,
``TEST_GH_TOKEN_RESEARCHER_A`` y, opcional, ``TEST_GH_TOKEN_RESEARCHER_B`` (12-mvp §3.3
y REQ-MVP-28; 00-marco D-67). Los tokens viven fuera del repo (00-marco D-54); sin
los dos primeros, el caso se omite, y sin el tercero se omiten solo las pruebas que
necesitan tres identidades distintas. Las
pruebas de inyección de fallas corren solo con el falso (REQ-FORGE-64) y usan
``fake_sandbox``.

``Sandbox`` reúne lo que cada prueba necesita preparar sin depender del
adaptador concreto: la rama base y su sha, los logins de cada papel
(``accounts``), una segunda cuenta y la creación de commits con contenido, PR y
revisiones (con el falso, por siembra; con GitHub, en H3). Las pruebas
compartidas comparan con ``sandbox.accounts`` y nunca con los logins fijos del
falso (REQ-FORGE-63): en el sandbox real las cuentas de máquina son otras.
"""

from __future__ import annotations

import itertools
import os
import secrets
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, TypeVar

import pytest

from aucove import _meta
from aucove.forge.base import FileChange, ForgeAdapter, ForgeError, PullRequest, RepoRef, Review
from aucove.forge.fake import ADAPTER_METHODS, Fault, FakeClock, FakeForge

T = TypeVar("T")

REPO = RepoRef("lab", "articulo")
START = "2026-09-29T12:00:00Z"
LEAD = "ana"  # papel lead (12-mvp REQ-MVP-28)
RESEARCHER_A = "bruno"
RESEARCHER_B = "carla"
READ_ONLY = "dario"  # sin push: para las escrituras que GitHub descarta o rechaza
BASE_FILES: dict[str, bytes] = {
    "roles.yaml": b"version: 1\nlead: ana\n",
    "tex/main.tex": b"\\documentclass{article}\n",
    "sections/methods/methods.tex": b"% methods\n",
}
# Sandbox real (08 §10.2; 00-marco D-68): la rama fija con un workflow para CT-GIT-03, creada
# una vez desde la web porque los tokens de la suite no tienen Workflows, y el ruleset mínimo
# que CT-CAP-01 necesita en la base de ct-public (borrado y push forzado).
WORKFLOW_FIXTURE = "ct-fixtures/workflow-base"
SANDBOX_RULESET = "ct-sandbox-base"
# En un repo público, GitHub abre Issues aunque el token no tenga el permiso Issues (CT-AUTH-01;
# 00-marco D-68). En uno privado, no.
OPEN_WITHOUT_PERMISSION = frozenset({"create_issue"})
LIVE_WAIT_S = 30.0  # las listas de GitHub tardan unos segundos en reflejar una escritura


@dataclass(frozen=True)
class Accounts:
    """Logins de las cuentas del sandbox por papel (12-mvp REQ-MVP-28)."""

    lead: str
    researcher_a: str
    researcher_b: str
    read_only: str


FAKE_ACCOUNTS = Accounts(LEAD, RESEARCHER_A, RESEARCHER_B, READ_ONLY)


class HttpProbe(Protocol):
    """Lo que CT-ETAG-01 lee de la capa HTTP del adaptador real (H3)."""

    def last_status(self) -> int: ...

    def rate_remaining(self) -> int: ...


@dataclass
class Sandbox:
    """Repo de pruebas listo: rama base sembrada y dos cuentas con push."""

    kind: str  # 'fake' o 'github'
    forge: ForgeAdapter  # cuenta lead
    other: ForgeAdapter  # cuenta researcher-a
    base_branch: str
    base_sha: str
    run_id: str
    fake: FakeForge | None
    accounts: Accounts = FAKE_ACCOUNTS
    live: LiveEnv | None = None  # solo con la forja real
    _seq: Iterator[int] = field(default_factory=lambda: itertools.count(1))

    def _adapter_for(self, login: str | None) -> ForgeAdapter:
        """Adaptador de la cuenta ``login`` en el sandbox real (lead si es ``None``)."""
        if login is None or login == self.accounts.lead:
            return self.forge
        if login == self.accounts.researcher_a:
            return self.other
        if self.live is not None and self.live.third is not None and login == self.accounts.researcher_b:
            return self.live.third
        pytest.skip(f"live: no hay token para {login}")

    @property
    def lead_login(self) -> str:
        return self.accounts.lead

    @property
    def researcher_a_login(self) -> str:
        return self.accounts.researcher_a

    @property
    def researcher_b_login(self) -> str:
        return self.accounts.researcher_b

    @property
    def has_researcher_b(self) -> bool:
        """``researcher-b`` es opcional en el sandbox real (12-mvp REQ-MVP-28; 00-marco D-67)."""
        return self.live is None or self.live.third is not None

    def require_researcher_b(self) -> None:
        """Omite, en el sandbox real con dos cuentas, una prueba que necesita tres identidades distintas."""
        if not self.has_researcher_b:
            pytest.skip(f"live: tres identidades distintas; define {TOKEN_VARS['researcher_b']} (00-marco D-67)")

    @property
    def second_assignee_login(self) -> str:
        """Otra persona además de ``researcher-a``: ``researcher-b``, o ``lead`` si no está (D-67)."""
        return self.accounts.researcher_b if self.has_researcher_b else self.accounts.lead

    def branch(self, suffix: str) -> str:
        """Rama propia de la corrida: ``ct/<run_id>/<suffix>`` (08 §10.2)."""
        return f"ct/{self.run_id}/{suffix}"

    def tick(self, seconds: float = 60) -> None:
        """Deja pasar el tiempo: con el falso avanza su reloj; con la forja real pasa solo."""
        if self.fake is not None:
            self.fake.clock.advance(seconds)

    def eventually(self, read: Callable[[], T], ready: Callable[[T], bool], *, timeout: float = LIVE_WAIT_S) -> T:
        """Relee hasta que ``ready`` se cumpla o venza el plazo; con el falso, una sola lectura.

        Las listas de GitHub (Issues, actividad de refs) tardan unos segundos en reflejar una
        escritura (00-marco D-68). Devuelve la última lectura: la prueba la verifica después.
        """
        value = read()
        if self.fake is not None:
            return value
        deadline = time.monotonic() + timeout
        while not ready(value) and time.monotonic() < deadline:
            time.sleep(2)
            value = read()
        return value

    def workflow_fixture(self) -> str:
        """Commit sobre la base que agrega ``.github/workflows/x.yml`` (CT-GIT-03).

        En el sandbox real es la punta de ``WORKFLOW_FIXTURE``; si la rama no existe, la prueba
        se omite. Con el falso se siembra directamente.
        """
        if self.fake is not None:
            return self.content_commit({".github/workflows/x.yml": b"on: push\n"})
        ref = self.forge.get_ref(WORKFLOW_FIXTURE)
        if ref is None:
            pytest.skip(f"live: crea desde la web la rama {WORKFLOW_FIXTURE} con .github/workflows/x.yml (D-68)")
        return ref.sha

    def claim_commit(
        self, parent: str | None = None, message: str | None = None, *, forge: ForgeAdapter | None = None
    ) -> str:
        """Commit sin cambios (commit de reclamo de 08 §4.4) sobre ``parent`` o la base."""
        adapter = forge or self.forge
        text = message or f"ct commit {next(self._seq)}"
        return adapter.create_empty_commit(parent or self.base_sha, text)

    def content_commit(
        self, changes: Mapping[str, bytes | None], *, parent: str | None = None, message: str | None = None
    ) -> str:
        """Commit con cambios de contenido sobre ``parent`` o la base, sin mover ramas; ``None`` borra."""
        if self.fake is None:  # forja real: commit_files sobre una rama propia de la corrida
            start = parent or self.base_sha
            scratch = self.branch(f"scratch-{next(self._seq)}")
            self.forge.create_ref(scratch, start)
            files = [FileChange(path, content) for path, content in changes.items()]
            text = message or f"ct content {next(self._seq)}"
            return self.forge.commit_files(scratch, expected_head=start, changes=files, message=text)
        start = parent or self.base_sha
        tree = self.fake.tree_of(start)
        assert tree is not None, f"unknown tree for {start}"
        for path, content in changes.items():
            if content is None:
                tree.pop(path, None)
            else:
                tree[path] = content
        text = message or f"ct content {next(self._seq)}"
        return self.fake.seed_commit(tree, parents=[start], message=text, author=self.lead_login)

    def make_pull_request(
        self,
        *,
        head_branch: str,
        head_sha: str | None = None,
        title: str = "ct pull request",
        body: str = "",
        author: str | None = None,
    ) -> PullRequest:
        """PR de ``head_branch`` contra la base; sus archivos son el diff real, como en GitHub."""
        if self.fake is None:  # forja real: la cabeza es la punta de head_branch
            adapter = self._adapter_for(author)
            return adapter.create_pull_request(head=head_branch, base=self.base_branch, title=title, body=body)
        return self.fake.seed_pull_request(
            head_branch=head_branch,
            head_sha=head_sha,
            title=title,
            body=body,
            author=author or self.lead_login,
            base_branch=self.base_branch,
        )

    def add_review(self, number: int, *, author: str, state: str, submitted_at: datetime | None = None) -> Review:
        if self.fake is None:  # forja real: submit_review con la fecha real
            event = {"APPROVED": "APPROVE", "CHANGES_REQUESTED": "REQUEST_CHANGES"}.get(state)
            if event is None or submitted_at is not None:
                pytest.skip("live: solo APPROVE y REQUEST_CHANGES, con la fecha de la forja")
            adapter = self._adapter_for(author)
            pr = adapter.get_pull_request(number)
            assert pr is not None
            operation = "approve" if event == "APPROVE" else "request-changes"
            return adapter.submit_review(
                number,
                event=event,  # type: ignore[arg-type]
                body="ct review",
                commit_sha=pr.head_sha,
                confirmation=_Confirmed(operation, datetime.now(timezone.utc)),
            )
        return self.fake.seed_review(number, author=author, state=state, submitted_at=submitted_at)  # type: ignore[arg-type]

    def invalid_token(self) -> ForgeAdapter:
        """Adaptador cuyo token la forja rechaza (401)."""
        if self.fake is None:
            assert self.live is not None
            return _github(self.live.repo, "ghp_" + "0" * 36)
        self.fake.faults.add(Fault("*", "http_401"))
        return self.forge

    @contextmanager
    def no_permission_token(self) -> Iterator[ForgeAdapter]:
        """Adaptador con un token fine-grained sin permisos sobre el repo (CT-AUTH-01).

        Con el falso, toda petición da 403 mientras dura el bloque, salvo las que GitHub admite
        sin permiso en un repo público (``OPEN_WITHOUT_PERMISSION``); al salir, las cuentas del
        sandbox vuelven a operar (para limpiar ramas e Issues).
        """
        if self.fake is None:
            assert self.live is not None
            yield _github(self.live.repo, _env_token("no_permission"))
            return
        allowed = OPEN_WITHOUT_PERMISSION if self.fake.capabilities().visibility == "public" else frozenset()
        for method in sorted(set(ADAPTER_METHODS) - allowed):
            self.fake.faults.add(Fault(method, "http_403"))
        try:
            yield self.forge
        finally:
            self.fake.faults.clear()

    def http_probe(self) -> HttpProbe:
        """Estado HTTP de la última respuesta y ``x-ratelimit-remaining`` del adaptador real (CT-ETAG-01)."""
        if self.fake is not None:
            raise NotImplementedError("el falso no modela HTTP (08 §7.4)")
        return _Probe(self.forge)

    def read_only(self) -> ForgeAdapter:
        """Cuenta con acceso de lectura y sin push."""
        if self.fake is None:
            assert self.live is not None
            return _github(self.live.repo, _env_token("read_only"))
        return self.fake.as_user(self.accounts.read_only)


def new_fake(profile: str = "enforced", *, accounts: Accounts = FAKE_ACCOUNTS, **options: object) -> FakeForge:
    fake = FakeForge(REPO, accounts.lead, profile=profile, clock=FakeClock(START), **options)  # type: ignore[arg-type]
    fake.seed_user(accounts.lead, permission="admin")
    fake.seed_user(accounts.researcher_a, permission="write")
    fake.seed_user(accounts.researcher_b, permission="write")
    fake.seed_user(accounts.read_only, permission="read")
    base = fake.seed_commit(BASE_FILES, message="base")
    fake.seed_branch(fake.default_branch, base)
    return fake


def fake_sandbox_of(fake: FakeForge, *, accounts: Accounts = FAKE_ACCOUNTS) -> Sandbox:
    base = fake.get_ref(fake.default_branch)
    assert base is not None
    fake.calls.clear()
    return Sandbox(
        "fake", fake, fake.as_user(accounts.researcher_a), fake.default_branch, base.sha, "fake", fake, accounts
    )


# -- forja real (08 §10.2; 12-mvp §3.3) ---------------------------------------------------

CONTRACT_REPO = _meta.ENV_PREFIX + "CONTRACT_REPO"  # owner/repo del sandbox, p. ej. <org>/ct-public
CONTRACT_TEAM_REPO = _meta.ENV_PREFIX + "CONTRACT_TEAM_REPO"  # ct-private-team de la org Team (D-24)
TOKEN_VARS: dict[str, str] = {
    "lead": "TEST_GH_TOKEN_LEAD",
    "researcher_a": "TEST_GH_TOKEN_RESEARCHER_A",
    "researcher_b": "TEST_GH_TOKEN_RESEARCHER_B",
    "read_only": "TEST_GH_TOKEN_READ_ONLY",
    "no_permission": "TEST_GH_TOKEN_NO_PERMISSION",
    "team_lead": "TEST_GH_TOKEN_TEAM_LEAD",
}


@dataclass(frozen=True)
class _Confirmed:
    """``HumanConfirmation`` de las revisiones del sandbox real (06-permisos §6)."""

    operation: str
    confirmed_at: datetime


class _Probe:
    def __init__(self, forge: ForgeAdapter) -> None:
        self._forge = forge

    def last_status(self) -> int:
        return int(getattr(self._forge, "last_status", 0) or 0)

    def rate_remaining(self) -> int:
        return int(getattr(self._forge, "last_rate_remaining", -1) or 0)


def _env_token(role: str) -> str:
    value = (os.environ.get(TOKEN_VARS[role]) or "").strip()
    if not value:
        pytest.skip(f"live: define {TOKEN_VARS[role]} (12-mvp §3.3; fuera del repo, D-54)")
    return value


def _env_repo(var: str) -> str:
    value = (os.environ.get(var) or "").strip()
    if not value:
        pytest.skip(f"live: define {var} (08 §10.2)")
    return value


def _github(repo: str, token: str) -> ForgeAdapter:
    from aucove.forge.github import GitHubAdapter, parse_repo

    return GitHubAdapter(parse_repo(repo), token=token, env={}, ci=False)


@dataclass
class LiveEnv:
    """Cuentas del sandbox real, una vez por proceso de pruebas."""

    repo: str
    lead: ForgeAdapter
    other: ForgeAdapter
    third: ForgeAdapter | None
    accounts: Accounts
    base_branch: str
    session: str
    seq: Iterator[int] = field(default_factory=lambda: itertools.count(1))


_LIVE: list[LiveEnv] = []


def _live_env() -> LiveEnv:
    if _LIVE:
        return _LIVE[0]
    repo = _env_repo(CONTRACT_REPO)
    lead = _github(repo, _env_token("lead"))
    other = _github(repo, _env_token("researcher_a"))
    third_token = (os.environ.get(TOKEN_VARS["researcher_b"]) or "").strip()
    third = _github(repo, third_token) if third_token else None  # opcional (12-mvp REQ-MVP-28; D-67)
    read_only = (os.environ.get(TOKEN_VARS["read_only"]) or "").strip()
    accounts = Accounts(
        lead.identity().login,
        other.identity().login,
        third.identity().login if third is not None else "",
        _github(repo, read_only).identity().login if read_only else "",
    )
    session = time.strftime("%Y%m%d%H%M%S", time.gmtime()) + "-" + secrets.token_hex(2)
    base_branch = lead.capabilities().default_branch
    _seed_base(lead, base_branch)
    env = LiveEnv(repo, lead, other, third, accounts, base_branch, session)
    _LIVE.append(env)
    return env


def _seed_base(lead: ForgeAdapter, base_branch: str) -> None:
    """Agrega a la base del sandbox real los ``BASE_FILES`` que le falten (08 §10.2; 00-marco D-68).

    Las pruebas compartidas esperan en la base los archivos que el falso siembra. Si ya están,
    no escribe nada; si no, un commit de ``lead`` y la espera a que la ref lo muestre.
    """
    head = lead.get_ref(base_branch)
    assert head is not None, f"live: {base_branch} no existe"
    changes = []
    for path, content in BASE_FILES.items():
        found = lead.get_file(path, head.sha)
        if found is None or found.content != content:
            changes.append(FileChange(path, content))
    if not changes:
        return
    sha = lead.commit_files(base_branch, expected_head=head.sha, changes=changes,
                            message="ct: archivos base del sandbox (08 §10.2)")
    deadline = time.monotonic() + LIVE_WAIT_S
    while (ref := lead.get_ref(base_branch)) is None or ref.sha != sha:
        assert time.monotonic() < deadline, f"live: {base_branch} no muestra {sha}"
        time.sleep(2)


def _ensure_sandbox_ruleset(repo: str, token: str, branch: str) -> None:
    """Ruleset mínimo de la base de ``repo`` para CT-CAP-01, con el token de ``lead`` (Administration).

    Prohíbe borrar la rama por defecto y los pushes forzados; no exige PR, así que la siembra
    de ``_seed_base`` sigue pudiendo escribir en la base. ``upsert_ruleset`` es idempotente;
    después espera a que ``rules/branches/{branch}`` muestre las reglas.
    """
    from aucove.forge.github import GitHubAdmin, parse_repo

    admin = GitHubAdmin(parse_repo(repo), token)
    try:
        admin.upsert_ruleset(SANDBOX_RULESET, {
            "target": "branch",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "bypass_actors": [],
            "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}],
        })
        deadline = time.monotonic() + LIVE_WAIT_S
        while not admin.branch_rules(branch):
            assert time.monotonic() < deadline, f"live: {repo} no muestra las reglas de {branch}"
            time.sleep(2)
    finally:
        admin.close()


def _github_sandbox() -> Iterator[Sandbox]:
    """Sandbox real con un ``run_id`` propio; al terminar borra sus ramas ``ct/<run_id>/…``."""
    env = _live_env()
    base = env.lead.get_ref(env.base_branch)
    assert base is not None
    sandbox = Sandbox(
        "github", env.lead, env.other, env.base_branch, base.sha, f"{env.session}-{next(env.seq)}", None,
        env.accounts, live=env,
    )
    yield sandbox
    for ref in env.lead.list_refs(f"ct/{sandbox.run_id}/"):
        try:
            env.lead.delete_ref(ref.branch)
        except ForgeError:
            pass  # la limpieza no cambia el resultado de la prueba


@pytest.fixture(params=["fake", pytest.param("github", marks=pytest.mark.live)])
def forge(request: pytest.FixtureRequest) -> Iterator[ForgeAdapter]:
    """Adaptador bajo prueba (12-mvp §3.3): el falso, o GitHub en ``live``."""
    if request.param == "github":
        gen = _github_sandbox()
        sandbox = next(gen)
        request.node._ct_sandbox = sandbox  # type: ignore[attr-defined]
        yield sandbox.forge
        for _ in gen:
            pass
        return
    yield new_fake()


@pytest.fixture
def sandbox(forge: ForgeAdapter, request: pytest.FixtureRequest) -> Sandbox:
    if isinstance(forge, FakeForge):
        return fake_sandbox_of(forge)
    return request.node._ct_sandbox  # type: ignore[no-any-return,attr-defined]


# Repos sandbox de 08 §10.2 por perfil: (repo, visibilidad, perfil esperado). ct-private-team
# es el repo privado de la org Team separada (00-marco D-24).
SANDBOX_REPOS: dict[str, tuple[str, str]] = {
    "ct-public": ("public", "enforced"),
    "ct-private-team": ("private", "enforced"),
    "ct-private": ("private", "advisory"),
}


@pytest.fixture(params=["fake", pytest.param("github", marks=pytest.mark.live)])
def repo_sandbox(request: pytest.FixtureRequest) -> Callable[[str], ForgeAdapter]:
    """Fábrica ``repo -> adaptador`` sobre cada sandbox de 08 §10.2 (CT-CAP-01, CT-CAP-02)."""
    if request.param == "github":

        def real(repo: str) -> ForgeAdapter:
            if repo == "ct-private-team":
                return _github(_env_repo(CONTRACT_TEAM_REPO), _env_token("team_lead"))
            owner = _env_repo(CONTRACT_REPO).split("/", 1)[0]
            adapter = _github(f"{owner}/{repo}", _env_token("lead"))
            if SANDBOX_REPOS[repo][1] == "enforced":  # la base de ct-public necesita reglas (D-68)
                _ensure_sandbox_ruleset(f"{owner}/{repo}", _env_token("lead"), adapter.capabilities().default_branch)
            return adapter

        return real

    def make(repo: str) -> ForgeAdapter:
        visibility, profile = SANDBOX_REPOS[repo]
        return new_fake(profile, visibility=visibility)

    return make


@pytest.fixture(params=[pytest.param("github", marks=pytest.mark.live)])
def live_sandbox(request: pytest.FixtureRequest) -> Iterator[Sandbox]:
    """Solo la forja real: mediciones de HTTP y de GitHub que el falso no modela (CT-ETAG-01, CT-GIT-03)."""
    yield from _github_sandbox()


@pytest.fixture
def fake_sandbox() -> Sandbox:
    """Sandbox solo con el falso, para fallas inyectadas y siembra (REQ-FORGE-64)."""
    return fake_sandbox_of(new_fake())


@pytest.fixture
def fake(fake_sandbox: Sandbox) -> FakeForge:
    assert fake_sandbox.fake is not None
    return fake_sandbox.fake
