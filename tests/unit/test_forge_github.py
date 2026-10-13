# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Adaptador GitHub sobre una API falsa (08-forja §3 a §8; ``forge/github.py``).

``Api`` responde por ``httpx2.MockTransport``: sin red, sin sockets y sin esperar los
reintentos (``sleep`` se registra). Las pruebas contra github.com están en
``tests/contract`` con el marcador ``live``.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest

from aucove import _meta
from aucove.forge import github as gh
from aucove.forge.base import (
    NULL_SHA,
    BaseNotDefault,
    FileChange,
    ForgeAdapter,
    ForgeAuthError,
    ForgeNotFound,
    ForgePermissionError,
    ForgeUnavailable,
    ForgeUnsupported,
    ForgeValidationError,
    Label,
    MergeConflict,
    NetworkTimeout,
    NetworkUnreachable,
    NoCommitsBetween,
    NoTokenError,
    PullRequestExists,
    RateLimited,
    Ref,
    RefAlreadyExists,
    RefConflict,
    RepoRef,
)
from aucove.forge.github import GitHubAdapter, create, parse_repo
from aucove.localstore.store import Store

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
REPO = RepoRef("lab", "articulo")
BASE = "/repos/lab/articulo"
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
FG = "github_pat_TESTTOKEN"


# ---------------------------------------------------------------------------
# API falsa
# ---------------------------------------------------------------------------


@dataclass
class Reply:
    status: int = 200
    body: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)
    raise_: BaseException | None = None
    raw: bytes | None = None

    def response(self, request: httpx2.Request) -> httpx2.Response:
        if self.raise_ is not None:
            raise self.raise_
        if self.raw is not None:
            return httpx2.Response(self.status, content=self.raw, headers=dict(self.headers))
        if self.body is None:
            return httpx2.Response(self.status, headers=dict(self.headers))
        return httpx2.Response(self.status, json=self.body, headers=dict(self.headers))


def _full(path: str) -> str:
    """Ruta completa: relativa a ``/repos/{o}/{r}`` salvo que empiece por ``/``."""
    if path.startswith("/"):
        return path
    return f"{BASE}/{path}" if path else BASE


class Api:
    """Rutas ``(método, ruta)`` -> respuestas en orden; la última se repite."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[Reply]] = {}
        self.requests: list[httpx2.Request] = []

    def on(self, method: str, path: str, *replies: Reply | int | Mapping[str, Any] | list[Any]) -> Api:
        out: list[Reply] = []
        for r in replies:
            if isinstance(r, Reply):
                out.append(r)
            elif isinstance(r, int):
                out.append(Reply(r, {"message": f"HTTP {r}"}))
            else:
                out.append(Reply(200, r))
        self.routes.setdefault((method, _full(path)), []).extend(out)
        return self

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        replies = self.routes.get((request.method, request.url.path))
        if not replies:
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        return reply.response(request)

    def calls(self, method: str | None = None, path: str | None = None) -> list[httpx2.Request]:
        full = None if path is None else _full(path)
        return [r for r in self.requests if (method is None or r.method == method) and (full is None or r.url.path == full)]

    def body(self, request: httpx2.Request) -> Any:
        return json.loads(request.content.decode("utf-8"))


@pytest.fixture(autouse=True)
def _unblock() -> None:
    gh._BLOCKED.clear()  # el bloqueo por límite es del proceso (REQ-FORGE-51)


def make(api: Api, *, token: str | None = FG, env: Mapping[str, str] | None = None, **kw: Any) -> GitHubAdapter:
    waits: list[float] = []
    kw.setdefault("base_branch", "main")
    kw.setdefault("ci", False)
    adapter = GitHubAdapter(
        REPO,
        env=env or {},
        token=token,
        http_transport=httpx2.MockTransport(api),
        now=lambda: NOW,
        sleep=waits.append,
        jitter=lambda: 0.5,
        **kw,
    )
    adapter.waits = waits  # type: ignore[attr-defined]
    return adapter


def user(login: str = "ana", uid: int = 7, kind: str = "User") -> dict[str, Any]:
    return {"login": login, "id": uid, "type": kind}


def ref_json(name: str, sha: str, namespace: str = "heads") -> dict[str, Any]:
    return {"ref": f"refs/{namespace}/{name}", "object": {"sha": sha, "type": "commit"}}


def issue_json(number: int, **over: Any) -> dict[str, Any]:
    d = {
        "number": number,
        "title": f"t{number}",
        "body": "",
        "state": "open",
        "state_reason": None,
        "labels": [],
        "assignees": [],
        "user": user(),
        "created_at": "2026-10-01T10:00:00Z",
        "updated_at": "2026-10-01T10:00:00Z",
        "closed_at": None,
        "html_url": f"https://github.com/lab/articulo/issues/{number}",
    }
    d.update(over)
    return d


def pull_json(number: int, *, head: str = "task/1", fork: bool = False, **over: Any) -> dict[str, Any]:
    d = {
        "number": number,
        "node_id": f"PR_{number}",
        "title": f"pr{number}",
        "body": "",
        "state": "open",
        "draft": False,
        "merged_at": None,
        "head": {"ref": head, "sha": SHA_A, "repo": {"full_name": "fork/articulo" if fork else "lab/articulo"}},
        "base": {"ref": "main", "sha": SHA_B},
        "user": user(),
        "labels": [],
        "requested_reviewers": [],
        "created_at": f"2026-10-0{min(number, 9)}T10:00:00Z",
        "updated_at": "2026-10-05T10:00:00Z",
        "html_url": f"https://github.com/lab/articulo/pull/{number}",
    }
    d.update(over)
    return d


def run_json(rid: int, **over: Any) -> dict[str, Any]:
    d = {
        "id": rid,
        "name": "seal",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "head_sha": SHA_A,
        "head_branch": "main",
        "actor": user("ana"),
        "html_url": f"https://github.com/lab/articulo/actions/runs/{rid}",
        "created_at": f"2026-10-0{rid % 9 + 1}T10:00:00Z",
    }
    d.update(over)
    return d


# ---------------------------------------------------------------------------
# Convenciones HTTP (§4.1)
# ---------------------------------------------------------------------------


def test_adapter_satisfies_the_protocol() -> None:
    assert isinstance(make(Api()), ForgeAdapter)


def test_required_headers_and_no_token_in_repr() -> None:
    api = Api().on("GET", "git/ref/heads/main", ref_json("main", SHA_A))
    adapter = make(api)
    assert adapter.get_ref("main") == Ref("main", SHA_A)
    req = api.requests[0]
    assert req.headers["Accept"] == "application/vnd.github+json"
    assert req.headers["X-GitHub-Api-Version"] == "2026-03-10"  # REQ-FORGE-22
    assert req.headers["User-Agent"] == f"{_meta.NAME}/{_meta.__version__}"
    assert req.headers["Authorization"] == f"Bearer {FG}"
    assert FG not in repr(adapter)


def test_paths_are_percent_encoded() -> None:
    api = Api().on("GET", "git/ref/heads/task/a b", 404)
    assert make(api).get_ref("task/a b") is None
    assert api.requests[0].url.raw_path == b"/repos/lab/articulo/git/ref/heads/task/a%20b"


def test_offline_flag_fails_without_requests() -> None:
    api = Api()
    adapter = make(api, env={_meta.ENV_PREFIX + "OFFLINE": "1"})
    with pytest.raises(NetworkUnreachable):
        adapter.get_ref("main")
    assert api.requests == []  # REQ-ARCH-63


def test_token_is_resolved_lazily() -> None:
    adapter = GitHubAdapter(REPO, env={}, http_transport=httpx2.MockTransport(Api()))
    with pytest.raises(NoTokenError):  # construir no falla; la primera petición sí
        adapter.get_ref("main")


def test_token_from_environment() -> None:
    api = Api().on("GET", "git/ref/heads/main", ref_json("main", SHA_A))
    adapter = GitHubAdapter(REPO, env={"GH_TOKEN": "ghp_envtoken"}, http_transport=httpx2.MockTransport(api))
    adapter.get_ref("main")
    assert api.requests[0].headers["Authorization"] == "Bearer ghp_envtoken"


def test_github_api_url_only_in_actions() -> None:
    env = {"GITHUB_API_URL": "https://ghe.example/api/v3"}
    assert make(Api(), env=env)._api_base == gh.API_URL
    assert make(Api(), env={**env, "GITHUB_ACTIONS": "true"})._api_base == "https://ghe.example/api/v3"


# ---------------------------------------------------------------------------
# Paginación (REQ-FORGE-8)
# ---------------------------------------------------------------------------


def _page(items: list[Any], nxt: str | None) -> Reply:
    headers = {"Link": f'<{nxt}>; rel="next", <https://api.github.com/x?page=99>; rel="last"'} if nxt else {}
    return Reply(200, items, headers)


def test_pages_follow_link_next() -> None:
    nxt = "https://api.github.com/repositories/1/git/matching-refs/heads/task/?per_page=100&page=2"
    api = Api()
    api.on("GET", "git/matching-refs/heads/task/", _page([ref_json("task/2", SHA_B)], nxt))
    api.on("GET", "/repositories/1/git/matching-refs/heads/task/", _page([ref_json("task/1", SHA_A)], None))
    assert make(api).list_refs("task/") == [Ref("task/1", SHA_A), Ref("task/2", SHA_B)]
    assert api.requests[0].url.params["per_page"] == "100"


def test_more_than_ten_pages_is_refused() -> None:
    nxt = "https://api.github.com/repos/lab/articulo/git/matching-refs/heads/task/?page=2"
    api = Api().on("GET", "git/matching-refs/heads/task/", _page([ref_json("task/1", SHA_A)], nxt))
    with pytest.raises(ForgeValidationError, match="truncated"):
        make(api).list_refs("task/")
    assert len(api.requests) == 10


def test_pull_request_files_allow_thirty_pages() -> None:
    nxt = "https://api.github.com/repos/lab/articulo/pulls/3/files?page=2"
    files = [{"filename": "a.tex", "status": "modified"}]
    api = Api().on("GET", "pulls/3/files", *([_page(files, nxt)] * 29), _page(files, None))
    assert len(make(api).list_pull_request_files(3)) == 30


def test_link_to_another_host_is_refused() -> None:
    api = Api().on("GET", "git/matching-refs/heads/task/", _page([], "https://evil.example/steal?page=2"))
    with pytest.raises(ForgeValidationError, match="outside"):
        make(api).list_refs("task/")


# ---------------------------------------------------------------------------
# Reintentos (§7.3)
# ---------------------------------------------------------------------------


def test_idempotent_request_retries_transient_errors() -> None:
    api = Api().on("GET", "git/ref/heads/main", 502, Reply(raise_=httpx2.ReadTimeout("slow")), ref_json("main", SHA_A))
    adapter = make(api)
    assert adapter.get_ref("main") == Ref("main", SHA_A)
    assert adapter.waits == [0.5, 1.0]  # type: ignore[attr-defined]


def test_interactive_gives_up_after_three_retries() -> None:
    api = Api().on("GET", "git/ref/heads/main", 503)
    adapter = make(api)
    with pytest.raises(ForgeUnavailable):
        adapter.get_ref("main")
    assert len(api.requests) == 4
    assert adapter.waits == [0.5, 1.0, 2.0]  # type: ignore[attr-defined]


def test_ci_retries_five_times() -> None:
    api = Api().on("GET", "git/ref/heads/main", 500)
    with pytest.raises(ForgeUnavailable):
        make(api, ci=True).get_ref("main")
    assert len(api.requests) == 6


def test_connection_refused_is_unreachable() -> None:
    api = Api().on("GET", "git/ref/heads/main", Reply(raise_=httpx2.ConnectError("refused")))
    with pytest.raises(NetworkUnreachable):
        make(api).get_ref("main")
    assert len(api.requests) == 4


def test_post_is_never_retried_blindly() -> None:
    # REQ-FORGE-29: un POST git/refs con 502 se confirma con un GET, sin repetirlo
    api = Api().on("POST", "git/refs", 502).on("GET", "git/ref/heads/task/1", 404)
    with pytest.raises(ForgeUnavailable):
        make(api).create_ref("task/1", SHA_A)
    assert len(api.calls("POST")) == 1  # el POST no se repite
    assert len(api.calls("GET")) == 3  # el GET de confirmación y sus dos relecturas (§4.4)


def test_operation_time_budget() -> None:
    api = Api().on("GET", "git/ref/heads/main", 502)
    adapter = make(api, budget_s=0.0001)
    adapter._deadline = 0.0  # ya vencido
    with pytest.raises(NetworkTimeout):
        adapter.get_ref("main")
    assert api.requests == []


# ---------------------------------------------------------------------------
# Límites (§7.3, REQ-FORGE-51)
# ---------------------------------------------------------------------------


def _limited(**headers: str) -> Reply:
    return Reply(403, {"message": "API rate limit exceeded"}, headers)


def test_primary_limit_raises_and_blocks_the_process() -> None:
    reset = int((NOW + timedelta(minutes=30)).timestamp())
    api = Api().on("GET", "git/ref/heads/main", _limited(**{"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}))
    adapter = make(api)
    with pytest.raises(RateLimited) as info:
        adapter.get_ref("main")
    assert info.value.reset_at == NOW + timedelta(minutes=30)
    with pytest.raises(RateLimited):
        make(api).get_ref("main")  # otro adaptador con el mismo token, mismo proceso
    assert len(api.requests) == 1


def test_short_retry_after_is_waited_once() -> None:
    api = Api().on("GET", "git/ref/heads/main", _limited(**{"retry-after": "3"}), ref_json("main", SHA_A))
    adapter = make(api)
    assert adapter.get_ref("main") == Ref("main", SHA_A)
    assert adapter.waits == [3.0]  # type: ignore[attr-defined]


def test_long_retry_after_is_rate_limited() -> None:
    api = Api().on("GET", "git/ref/heads/main", _limited(**{"retry-after": "30"}))
    with pytest.raises(RateLimited) as info:
        make(api).get_ref("main")
    assert info.value.reset_at == NOW + timedelta(seconds=30)


def test_secondary_limit_interactive_and_ci() -> None:
    secondary = Reply(403, {"message": "You have exceeded a secondary rate limit."}, {"x-ratelimit-remaining": "4000"})
    api = Api().on("GET", "git/ref/heads/main", secondary)
    with pytest.raises(RateLimited) as info:
        make(api).get_ref("main")
    assert info.value.reset_at == NOW + timedelta(seconds=60)
    gh._BLOCKED.clear()
    api = Api().on("GET", "git/ref/heads/main", secondary, secondary, secondary, ref_json("main", SHA_A))
    adapter = make(api, ci=True)
    assert adapter.get_ref("main") == Ref("main", SHA_A)
    assert adapter.waits == [60.0, 120.0, 240.0]  # type: ignore[attr-defined]


def test_ci_waits_for_a_close_primary_reset() -> None:
    reset = int((NOW + timedelta(minutes=10)).timestamp())
    limited = _limited(**{"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)})
    api = Api().on("GET", "git/ref/heads/main", limited, ref_json("main", SHA_A))
    adapter = make(api, ci=True)
    assert adapter.get_ref("main") == Ref("main", SHA_A)
    assert adapter.waits == [601.0]  # type: ignore[attr-defined]


def test_forbidden_without_limit_signal_is_permission_with_hint() -> None:
    reply = Reply(403, {"message": "Resource not accessible"}, {"X-Accepted-GitHub-Permissions": "contents=write"})
    api = Api().on("PATCH", "git/refs/heads/task/1", reply)
    adapter = make(api, hint=lambda key, **p: f"{key}:{p}")
    with pytest.raises(ForgePermissionError) as info:
        adapter.update_ref("task/1", SHA_A)
    assert info.value.hint is not None and "contents=write" in info.value.hint
    api = Api().on("POST", "git/refs", Reply(403, {"message": "Resource not accessible"}))
    with pytest.raises(ForgePermissionError) as info:
        make(api).create_ref("task/1", SHA_A)  # sin traductor: la cabecera tal cual (08 §2.1)
    assert info.value.hint == "X-Accepted-GitHub-Permissions: contents=write"


def test_unauthorized_invalidates_identity() -> None:
    api = Api().on("GET", "/user", user(), Reply(401, {"message": "Bad credentials"}))
    api.on("GET", "/user/repos", [{"id": 1}])
    adapter = make(api)
    assert adapter.identity().login == "ana"
    with pytest.raises(ForgeAuthError):
        adapter.identity(refresh=True)
    assert adapter._identity is None  # REQ-FORGE-19


# ---------------------------------------------------------------------------
# ETag (REQ-FORGE-52)
# ---------------------------------------------------------------------------


def test_etag_revalidation_returns_the_stored_body(tmp_path: Path) -> None:
    etag = {"ETag": '"abc"', "x-ratelimit-remaining": "4999"}
    api = Api().on(
        "GET",
        "git/ref/heads/main",
        Reply(200, ref_json("main", SHA_A), etag),
        Reply(304, None, {"ETag": '"abc"', "x-ratelimit-remaining": "4999"}),
    )
    adapter = make(api, cache=Store(tmp_path))
    assert adapter.get_ref("main") == Ref("main", SHA_A)
    second = make(api, cache=Store(tmp_path))  # otro proceso: la entrada está en disco
    assert second.get_ref("main") == Ref("main", SHA_A)
    assert api.requests[1].headers["If-None-Match"] == '"abc"'
    assert second.last_status == 304 and second.last_rate_remaining == 4999


def test_responses_without_validators_are_not_cached() -> None:
    api = Api().on("GET", "git/ref/heads/main", ref_json("main", SHA_A))
    adapter = make(api)
    adapter.get_ref("main")
    adapter.get_ref("main")
    assert "If-None-Match" not in api.requests[1].headers


# ---------------------------------------------------------------------------
# Identidad (§6)
# ---------------------------------------------------------------------------


def test_identity_fine_grained_scope_is_measured_on_refresh(tmp_path: Path) -> None:
    # 08 §5.2: GET /user/repos lo hace doctor (refresh=True); las demás operaciones no lo pagan
    api = Api().on("GET", "/user", user("Ana-Garcia", 5829301)).on("GET", "/user/repos", [{"id": 1}])
    ident = make(api, cache=Store(tmp_path)).identity()
    assert (ident.login, ident.id, ident.kind) == ("Ana-Garcia", 5829301, "user")
    assert (ident.token_kind, ident.token_scope, ident.oauth_scopes) == ("fine_grained_pat", "unknown", None)
    assert not api.calls("GET", "/user/repos")
    assert make(api, cache=Store(tmp_path)).identity(refresh=True).token_scope == "repo_restricted"
    later = GitHubAdapter(  # vencida la identidad, se renueva sin medir y conserva lo medido
        REPO, token=FG, ci=False, cache=Store(tmp_path), http_transport=httpx2.MockTransport(api),
        now=lambda: NOW + timedelta(hours=25),
    )
    assert later.identity().token_scope == "repo_restricted"
    api = Api().on("GET", "/user", user()).on("GET", "/user/repos", [{"id": 1}, {"id": 2}])
    assert make(api).identity(refresh=True).token_scope == "owner_wide"


def test_server_time_is_the_date_header_of_the_last_response() -> None:
    # 05 §9.19: expire-claims mide el vencimiento con la hora del servidor, no con el reloj local
    api = Api().on("GET", "/user", Reply(200, user(), {"Date": "Thu, 22 Oct 2026 06:17:03 GMT"}))
    adapter = make(api)
    assert adapter.server_time() is None  # todavía no hubo respuestas
    adapter.identity()
    assert adapter.server_time() == datetime(2026, 10, 22, 6, 17, 3, tzinfo=timezone.utc)


def test_identity_classic_token_reads_oauth_scopes() -> None:
    api = Api().on("GET", "/user", Reply(200, user(), {"X-OAuth-Scopes": "repo, workflow"}))
    ident = make(api, token="ghp_classic").identity()
    assert ident.token_scope == "broad" and ident.oauth_scopes == ("repo", "workflow")


def test_identity_bot() -> None:
    api = Api().on("GET", "/user", user("helper[bot]", 9, "Bot")).on("GET", "/user/repos", [])
    assert make(api).identity().kind == "bot"


def test_identity_in_ci_makes_no_request() -> None:
    api = Api()
    env = {"GITHUB_ACTIONS": "true", "GITHUB_RUN_ID": "77"}
    ident = make(api, token="ghs_job", env=env).identity()
    assert (ident.login, ident.id, ident.kind, ident.token_scope) == ("github-actions[bot]", None, "ci", "ci")
    assert api.requests == []  # REQ-FORGE-43


def test_identity_cache_on_disk_and_login_change(tmp_path: Path) -> None:
    api = Api().on("GET", "/user", user("ana", 7)).on("GET", "/user/repos", [{"id": 1}])
    make(api, cache=Store(tmp_path)).identity()
    assert make(api, cache=Store(tmp_path)).identity().login == "ana"
    assert len(api.calls("GET", "/user")) == 1  # vigente 24 h
    api = Api().on("GET", "/user", user("ana-renamed", 7)).on("GET", "/user/repos", [{"id": 1}])
    ident = make(api, cache=Store(tmp_path)).identity(refresh=True)
    assert (ident.login, ident.previous_login) == ("ana-renamed", "ana")  # §6.4


# ---------------------------------------------------------------------------
# Capacidades (§3)
# ---------------------------------------------------------------------------


def _repo(visibility: str = "public", owner_type: str = "Organization", **perms: bool) -> dict[str, Any]:
    return {
        "visibility": visibility,
        "private": visibility != "public",
        "default_branch": "main",
        "owner": {"login": "lab", "type": owner_type},
        "permissions": {"admin": False, "maintain": False, "push": True, "triage": True, "pull": True, **perms},
    }


def _caps_api(repo: dict[str, Any]) -> Api:
    return Api().on("GET", "/user", user()).on("GET", "/user/repos", [{"id": 1}]).on("GET", "", repo)


RULES = [
    {"type": "pull_request", "parameters": {
        "required_approving_review_count": 1, "require_code_owner_review": True,
        "require_last_push_approval": True, "dismiss_stale_reviews_on_push": True}},
    {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "aucove/build"}]}},
    {"type": "non_fast_forward"},
]


def test_public_repository_is_enforced_with_ruleset_rules() -> None:
    api = _caps_api(_repo(admin=True)).on("GET", "rules/branches/main", RULES)
    caps = make(api).capabilities()
    assert (caps.profile, caps.rules_available, caps.push_rulesets, caps.viewer_permission) == (
        "enforced", True, False, "admin"
    )
    assert caps.base_rules is not None and caps.base_rules.source == "ruleset"
    assert caps.base_rules.required_approvals == 1 and caps.base_rules.blocks_force_push is True
    assert caps.base_rules.required_checks == ("aucove/build",)
    assert caps.checks_api is False  # token fine-grained (§4.6)
    assert caps.evidence[0] == "GET /repos/lab/articulo -> 200 visibility=public"


def test_private_free_organization_is_advisory() -> None:
    api = _caps_api(_repo("private")).on("GET", "/orgs/lab", {"login": "lab", "plan": {"name": "free"}})
    caps = make(api).capabilities()
    assert (caps.profile, caps.rules_available, caps.base_rules) == ("advisory", False, None)
    assert "plan=free" in caps.evidence


def test_private_team_organization_is_enforced() -> None:
    api = _caps_api(_repo("private")).on("GET", "/orgs/lab", {"login": "lab", "plan": {"name": "team"}})
    api.on("GET", "rules/branches/main", []).on("GET", "branches/main", {"name": "main", "protected": False})
    adapter = make(api)
    caps = adapter.capabilities()
    assert (caps.profile, caps.push_rulesets) == ("enforced", True)
    assert caps.base_rules is not None and caps.base_rules.source == "none"
    assert "W_FORGE_RULES_NOT_APPLIED" in adapter.notices


def test_plan_not_visible_and_probe_upgrade_is_advisory() -> None:
    api = _caps_api(_repo("private")).on("GET", "/orgs/lab", {"login": "lab"})
    api.on("GET", "rulesets", Reply(403, {"message": "Upgrade to GitHub Pro or make this repository public"}))
    caps = make(api).capabilities()
    assert (caps.profile, caps.rules_available) == ("advisory", False)


def test_plan_not_visible_and_probe_200_is_unknown() -> None:
    api = _caps_api(_repo("private")).on("GET", "/orgs/lab", 404).on("GET", "rulesets", [])
    api.on("GET", "rules/branches/main", []).on("GET", "branches/main", {"name": "main", "protected": False})
    adapter = make(api)
    caps = adapter.capabilities()
    assert (caps.profile, caps.rules_available) == ("advisory", None)  # REQ-FORGE-14
    assert "W_FORGE_CAPABILITY_UNKNOWN" in adapter.notices


def test_classic_protection() -> None:
    branch = {"name": "main", "protected": True, "protection": {"required_status_checks": {"contexts": ["build"]}}}
    api = _caps_api(_repo()).on("GET", "rules/branches/main", []).on("GET", "branches/main", branch)
    rules = make(api).capabilities().base_rules
    assert rules is not None and (rules.source, rules.required_checks, rules.required_approvals) == ("classic", ("build",), None)


def test_base_not_default() -> None:
    api = _caps_api({**_repo(), "default_branch": "master"})
    with pytest.raises(BaseNotDefault) as info:
        make(api).capabilities()
    assert info.value.default_branch == "master" and info.value.kind == "validation"


def test_capabilities_cache_and_revalidation(tmp_path: Path) -> None:
    api = _caps_api(_repo()).on("GET", "rules/branches/main", RULES)
    make(api, cache=Store(tmp_path)).capabilities()
    detections = len(api.requests)
    later = make(api, cache=Store(tmp_path))
    assert later.capabilities().profile == "enforced"
    assert len(api.requests) == detections + 0  # vigente y revalidada hace menos de 10 min
    api2 = _caps_api(_repo()).on("GET", "rules/branches/main", RULES)
    stale = GitHubAdapter(
        REPO, token=FG, base_branch="main", ci=False, cache=Store(tmp_path),
        http_transport=httpx2.MockTransport(api2), now=lambda: NOW + timedelta(minutes=11),
    )
    stale.capabilities()
    assert [r.url.path for r in api2.requests] == [BASE]  # solo GET /repos/{o}/{r} (REQ-FORGE-20)


def test_capabilities_refresh_redetects(tmp_path: Path) -> None:
    api = _caps_api(_repo()).on("GET", "rules/branches/main", RULES)
    adapter = make(api, cache=Store(tmp_path))
    adapter.capabilities()
    before = len(api.calls("GET", "rules/branches/main"))
    adapter.capabilities(refresh=True)  # doctor, init y sync-roles (REQ-FORGE-18)
    assert len(api.calls("GET", "rules/branches/main")) == before + 1


def test_not_found_with_cached_capabilities_redetects_once(tmp_path: Path) -> None:
    api = _caps_api(_repo()).on("GET", "rules/branches/main", RULES)
    make(api, cache=Store(tmp_path)).capabilities()
    api.on("GET", "git/matching-refs/heads/task/", 404)
    adapter = make(api, cache=Store(tmp_path))
    adapter.capabilities()  # de la caché en disco
    before = len(api.calls("GET", ""))
    for _ in range(2):
        with pytest.raises(ForgeNotFound):
            adapter.list_refs("task/")
    assert len(api.calls("GET", "")) == before + 1  # una sola redetección (REQ-FORGE-19)


# ---------------------------------------------------------------------------
# Refs (§4.2, §4.4)
# ---------------------------------------------------------------------------


def test_create_ref_success_and_body() -> None:
    api = Api().on("POST", "git/refs", Reply(201, ref_json("task/1", SHA_A)))
    assert make(api).create_ref("task/1", SHA_A) == Ref("task/1", SHA_A)
    assert api.body(api.requests[0]) == {"ref": "refs/heads/task/1", "sha": SHA_A}


@pytest.mark.parametrize("status", [409, 422])
def test_create_ref_existing_with_other_sha(status: int) -> None:
    api = Api().on("POST", "git/refs", Reply(status, {"message": "Reference already exists"}))
    api.on("GET", "git/ref/heads/task/1", ref_json("task/1", SHA_B))
    with pytest.raises(RefAlreadyExists) as info:
        make(api).create_ref("task/1", SHA_A)
    assert info.value.existing == Ref("task/1", SHA_B)  # REQ-FORGE-28


def test_create_ref_existing_with_same_sha_is_success() -> None:
    api = Api().on("POST", "git/refs", 422).on("GET", "git/ref/heads/task/1", ref_json("task/1", SHA_A))
    assert make(api).create_ref("task/1", SHA_A) == Ref("task/1", SHA_A)


def test_create_ref_invalid_and_missing() -> None:
    api = Api().on("POST", "git/refs", 422).on("GET", "git/ref/heads/task/1", 404)
    adapter = make(api)
    with pytest.raises(ForgeValidationError):
        adapter.create_ref("task/1", SHA_A)
    assert len(api.calls("GET")) == 3 and adapter.waits == [0.5, 1.0]  # dos relecturas y se rinde (§4.4)


def test_create_ref_timeout_is_confirmed() -> None:
    api = Api().on("POST", "git/refs", Reply(raise_=httpx2.ReadTimeout("lost")))
    api.on("GET", "git/ref/heads/task/1", ref_json("task/1", SHA_A))
    assert make(api).create_ref("task/1", SHA_A) == Ref("task/1", SHA_A)
    api = Api().on("POST", "git/refs", Reply(raise_=httpx2.ReadTimeout("lost"))).on("GET", "git/ref/heads/task/1", 404)
    with pytest.raises(NetworkTimeout):
        make(api).create_ref("task/1", SHA_A)


def test_create_tag_ref() -> None:
    api = Api().on("POST", "git/refs", Reply(201, ref_json("archive/task-1-1", SHA_A, "tags")))
    assert make(api).create_ref("archive/task-1-1", SHA_A, namespace="tags") == Ref("archive/task-1-1", SHA_A)
    assert api.body(api.requests[0])["ref"] == "refs/tags/archive/task-1-1"


def test_update_ref_fast_forward_and_conflict() -> None:
    api = Api().on("PATCH", "git/refs/heads/task/1", ref_json("task/1", SHA_B))
    assert make(api).update_ref("task/1", SHA_B) == Ref("task/1", SHA_B)
    assert api.body(api.requests[0]) == {"sha": SHA_B, "force": False}  # REQ-FORGE-31
    api = Api().on("PATCH", "git/refs/heads/task/1", 422).on("GET", "git/ref/heads/task/1", ref_json("task/1", SHA_C))
    with pytest.raises(RefConflict) as info:
        make(api).update_ref("task/1", SHA_B)
    assert info.value.current == SHA_C
    api = Api().on("PATCH", "git/refs/heads/task/1", 409)
    with pytest.raises(ForgeUnavailable):
        make(api).update_ref("task/1", SHA_B)


def test_delete_ref() -> None:
    api = Api().on("DELETE", "git/refs/heads/task/1", Reply(204))
    assert make(api).delete_ref("task/1") is True
    api = Api().on("DELETE", "git/refs/heads/task/1", 422).on("GET", "git/ref/heads/task/1", 404)
    assert make(api).delete_ref("task/1") is False
    api = Api().on("DELETE", "git/refs/heads/task/1", 422).on("GET", "git/ref/heads/task/1", ref_json("task/1", SHA_A))
    with pytest.raises(ForgeValidationError):
        make(api).delete_ref("task/1")


def test_confirmation_rereads_a_ref_that_does_not_show_the_write_yet() -> None:
    """§4.4: justo después de escribir, GitHub a veces devuelve todavía la ref anterior (CT-REF-04)."""
    api = Api().on("POST", "git/refs", 422).on("GET", "git/ref/heads/task/1", 404, ref_json("task/1", SHA_B))
    adapter = make(api)
    with pytest.raises(RefAlreadyExists) as info:
        adapter.create_ref("task/1", SHA_A)
    assert info.value.existing == Ref("task/1", SHA_B)  # REQ-FORGE-28, aunque el primer GET no la viera
    assert len(api.calls("POST")) == 1 and len(api.calls("GET")) == 2  # se repite el GET, no el POST
    assert adapter.waits == [0.5]
    api = Api().on("PATCH", "git/refs/heads/task/1", 422)
    api.on("GET", "git/ref/heads/task/1", 404, ref_json("task/1", SHA_C))
    with pytest.raises(RefConflict) as conflict:
        make(api).update_ref("task/1", SHA_B)
    assert conflict.value.current == SHA_C
    api = Api().on("DELETE", "git/refs/heads/task/1", 422)
    api.on("GET", "git/ref/heads/task/1", ref_json("task/1", SHA_A), 404)
    assert make(api).delete_ref("task/1") is False


def test_confirmation_of_a_lost_response_rereads_too() -> None:
    api = Api().on("POST", "git/refs", Reply(raise_=httpx2.ReadTimeout("lost")))
    api.on("GET", "git/ref/heads/task/1", 404, ref_json("task/1", SHA_A))
    assert make(api).create_ref("task/1", SHA_A) == Ref("task/1", SHA_A)
    api = Api().on("PATCH", "git/refs/heads/task/1", Reply(raise_=httpx2.ReadTimeout("lost")))
    api.on("GET", "git/ref/heads/task/1", ref_json("task/1", SHA_A), ref_json("task/1", SHA_B))
    assert make(api).update_ref("task/1", SHA_B) == Ref("task/1", SHA_B)
    api = Api().on("DELETE", "git/refs/heads/task/1", 502)
    api.on("GET", "git/ref/heads/task/1", ref_json("task/1", SHA_A), 404)
    assert make(api).delete_ref("task/1") is True


def test_ref_activity_since_last_creation() -> None:
    page = [
        {"id": 4, "activity_type": "push", "actor": user("bruno"), "timestamp": "2026-10-04T10:00:00Z",
         "before": SHA_B, "after": SHA_C},
        {"id": 3, "activity_type": "branch_creation", "actor": user("ana"), "timestamp": "2026-10-03T10:00:00Z",
         "before": NULL_SHA, "after": SHA_B},
        {"id": 2, "activity_type": "branch_deletion", "actor": user("ana"), "timestamp": "2026-10-02T10:00:00Z",
         "before": SHA_A, "after": NULL_SHA},
    ]
    api = Api().on("GET", "activity", page)
    acts = make(api).list_ref_activity("task/1")
    assert acts is not None and [a.activity_type for a in acts] == ["branch_creation", "push"]
    assert api.requests[0].url.params["ref"] == "refs/heads/task/1"
    assert make(Api().on("GET", "activity", 404)).list_ref_activity("task/1") is None


# ---------------------------------------------------------------------------
# Commits y contenido (§4.2, §4.5)
# ---------------------------------------------------------------------------


def test_create_empty_commit_uses_the_parent_tree() -> None:
    api = Api().on("GET", f"git/commits/{SHA_A}", {"sha": SHA_A, "tree": {"sha": "t" * 40}})
    api.on("POST", "git/commits", Reply(201, {"sha": SHA_B}))
    assert make(api).create_empty_commit(SHA_A, "claim\n\nTask: #1") == SHA_B
    assert api.body(api.calls("POST")[0]) == {"message": "claim\n\nTask: #1", "tree": "t" * 40, "parents": [SHA_A]}


def test_commit_files_text_binary_and_delete() -> None:
    api = Api().on("GET", f"git/commits/{SHA_A}", {"sha": SHA_A, "tree": {"sha": "t" * 40}})
    api.on("POST", "git/blobs", Reply(201, {"sha": "d" * 40}))
    api.on("POST", "git/trees", Reply(201, {"sha": "e" * 40}))
    api.on("POST", "git/commits", Reply(201, {"sha": SHA_B}))
    api.on("PATCH", "git/refs/heads/task/1", ref_json("task/1", SHA_B))
    changes = [FileChange("a.tex", b"hola\n"), FileChange("img.png", b"\x89PNG\x00"), FileChange("old.tex", None),
               FileChange("run.sh", b"echo\n", executable=True)]
    sha = make(api).commit_files("task/1", expected_head=SHA_A, changes=changes, message="m", extra_parents=[SHA_C])
    assert sha == SHA_B
    tree = api.body(api.calls("POST", "git/trees")[0])
    assert tree["base_tree"] == "t" * 40
    assert tree["tree"] == [
        {"path": "a.tex", "mode": "100644", "type": "blob", "content": "hola\n"},
        {"path": "img.png", "mode": "100644", "type": "blob", "sha": "d" * 40},
        {"path": "old.tex", "mode": "100644", "type": "blob", "sha": None},
        {"path": "run.sh", "mode": "100755", "type": "blob", "content": "echo\n"},
    ]
    assert api.body(api.calls("POST", "git/commits")[0])["parents"] == [SHA_A, SHA_C]


def test_commit_files_rejects_workflows_and_bad_paths() -> None:
    api = Api()
    for path in (".github/workflows/x.yml", "../x", "/abs", "a\\b"):
        with pytest.raises(ForgeValidationError):
            make(api).commit_files("task/1", expected_head=SHA_A, changes=[FileChange(path, b"x")], message="m")
    assert api.requests == []  # REQ-FORGE-32


def test_commit_files_retry_already_applied() -> None:
    api = Api().on("GET", f"git/commits/{SHA_A}", {"sha": SHA_A, "tree": {"sha": "t" * 40}})
    api.on("POST", "git/trees", Reply(201, {"sha": "e" * 40})).on("POST", "git/commits", Reply(201, {"sha": SHA_B}))
    api.on("PATCH", "git/refs/heads/task/1", 422).on("GET", "git/ref/heads/task/1", ref_json("task/1", SHA_B))
    assert make(api).commit_files("task/1", expected_head=SHA_A, changes=[FileChange("a", b"x")], message="m") == SHA_B


def test_commit_files_rereads_a_branch_that_does_not_show_the_write_yet() -> None:
    api = Api().on("GET", f"git/commits/{SHA_A}", {"sha": SHA_A, "tree": {"sha": "t" * 40}})
    api.on("POST", "git/trees", Reply(201, {"sha": "e" * 40})).on("POST", "git/commits", Reply(201, {"sha": SHA_B}))
    api.on("PATCH", "git/refs/heads/task/1", 422).on("GET", "git/ref/heads/task/1", 404, ref_json("task/1", SHA_B))
    assert make(api).commit_files("task/1", expected_head=SHA_A, changes=[FileChange("a", b"x")], message="m") == SHA_B


def test_merge_into() -> None:
    api = Api().on("POST", "merges", Reply(201, {"sha": SHA_C}))
    assert make(api).merge_into("task/1", "main", message="update") == SHA_C
    assert api.body(api.requests[0]) == {"base": "task/1", "head": "main", "commit_message": "update"}
    assert make(Api().on("POST", "merges", Reply(204))).merge_into("task/1", "main", message="m") is None
    with pytest.raises(MergeConflict):
        make(Api().on("POST", "merges", 409)).merge_into("task/1", "main", message="m")
    with pytest.raises(ForgeNotFound):
        make(Api().on("POST", "merges", 404)).merge_into("task/1", "main", message="m")


def test_get_commit() -> None:
    data = {"sha": SHA_A, "author": None, "commit": {
        "message": "m", "author": {"date": "2026-10-01T10:00:00Z"}, "committer": {"date": "2026-10-01T11:00:00Z"}}}
    info = make(Api().on("GET", f"commits/{SHA_A}", data)).get_commit(SHA_A)
    assert (info.sha, info.author_login, info.committer_date.hour) == (SHA_A, None, 11)
    with pytest.raises(ForgeNotFound):
        make(Api().on("GET", f"commits/{SHA_A}", 422)).get_commit(SHA_A)


def test_get_file_small_big_and_missing() -> None:
    import base64

    small = {"type": "file", "sha": "f" * 40, "size": 4, "encoding": "base64", "content": base64.b64encode(b"hola").decode()}
    api = Api().on("GET", "contents/roles.yaml", small)
    got = make(api).get_file("roles.yaml", "main")
    assert got is not None and (got.content, got.blob_sha, got.ref) == (b"hola", "f" * 40, "main")
    assert api.requests[0].url.params["ref"] == "main"
    big = {"type": "file", "sha": "f" * 40, "size": 2 << 20, "encoding": "none", "content": ""}
    api = Api().on("GET", "contents/big.pdf", Reply(200, big), Reply(200, raw=b"%PDF-raw"))
    got = make(api).get_file("big.pdf", "main")
    assert got is not None and got.content == b"%PDF-raw"
    assert api.requests[1].headers["Accept"] == "application/vnd.github.raw+json"
    assert make(Api().on("GET", "contents/nope", 404)).get_file("nope", "main") is None
    assert make(Api().on("GET", "contents/sections", [{"type": "dir"}])).get_file("sections", "main") is None


def test_compare() -> None:
    data = {"status": "ahead", "ahead_by": 2, "behind_by": 0, "merge_base_commit": {"sha": SHA_A},
            "files": [{"filename": "b.tex", "status": "renamed", "previous_filename": "a.tex"}], "commits": []}
    api = Api().on("GET", "compare/main...task/1", data)
    cmp = make(api).compare("main", "task/1")
    assert (cmp.status, cmp.ahead_by, cmp.merge_base_sha, cmp.files_truncated) == ("ahead", 2, SHA_A, False)
    assert cmp.files[0].previous_path == "a.tex"
    assert api.requests[0].url.params["per_page"] == "1"
    make(api).compare("main", "task/1", with_commits=True)
    assert "per_page" not in api.requests[1].url.params


def test_codeowners_errors() -> None:
    data = {"errors": [{"line": 3, "kind": "Unknown owner", "message": "Unknown owner on line 3"}]}
    assert make(Api().on("GET", "codeowners/errors", data)).codeowners_errors("main") == ["Unknown owner on line 3"]
    assert make(Api().on("GET", "codeowners/errors", 404)).codeowners_errors("main") == []


# ---------------------------------------------------------------------------
# Issues, etiquetas y asignaciones (§4.2, §4.7)
# ---------------------------------------------------------------------------


def test_get_issue_rejects_pull_requests() -> None:
    api = Api().on("GET", "issues/3", issue_json(3, pull_request={"url": "x"}))
    with pytest.raises(ForgeValidationError):  # REQ-FORGE-9
        make(api).get_issue(3)
    assert make(Api().on("GET", "issues/4", 410)).get_issue(4) is None


def test_list_issues_drops_pull_requests_and_sorts() -> None:
    items = [issue_json(1), issue_json(2, pull_request={}), issue_json(3, created_at="2026-10-03T10:00:00Z")]
    api = Api().on("GET", "issues", items)
    issues = make(api).list_issues(labels=["state:open", "a b"], state="open", since=NOW)
    assert [i.number for i in issues] == [3, 1]
    params = api.requests[0].url.params
    assert (params["state"], params["labels"], params["since"]) == ("open", "state:open,a b", "2026-10-08T12:00:00Z")
    with pytest.raises(ForgeValidationError):
        make(Api()).list_issues(state="weird")


def test_create_issue_appends_marker_and_checks_labels() -> None:
    api = Api().on("POST", "issues", Reply(201, issue_json(9, labels=[{"name": "State:Proposed"}])))
    issue = make(api).create_issue("t", "cuerpo", labels=["state:proposed"], op_id="task-propose-1")
    assert issue.number == 9
    sent = api.body(api.requests[0])
    assert sent["body"] == f"cuerpo\n\n<!-- {_meta.NAME}:op v1 id=task-propose-1 -->"
    api = Api().on("POST", "issues", Reply(201, issue_json(9)))
    with pytest.raises(ForgeValidationError):  # REQ-FORGE-11: etiquetas descartadas
        make(api).create_issue("t", "b", labels=["state:proposed"], op_id="x")


def test_create_issue_recovers_after_timeout() -> None:
    marker = f"<!-- {_meta.NAME}:op v1 id=op-7 -->"
    found = issue_json(12, body=f"b\n\n{marker}", created_at="2026-10-08T12:00:01Z")
    api = Api().on("POST", "issues", Reply(raise_=httpx2.ReadTimeout("lost")))
    api.on("GET", "/user", user()).on("GET", "/user/repos", [{"id": 1}]).on("GET", "issues", [found])
    issue = make(api).create_issue("t", "b", op_id="op-7")
    assert issue.number == 12 and len(api.calls("POST")) == 1  # REQ-FORGE-35
    assert api.calls("GET", "issues")[0].url.params["creator"] == "ana"


def test_update_issue_payload() -> None:
    api = Api().on("PATCH", "issues/5", issue_json(5, state="closed", state_reason="not_planned"))
    issue = make(api).update_issue(5, state="closed", state_reason="not_planned")
    assert (issue.state, issue.state_reason) == ("closed", "not_planned")
    assert api.body(api.requests[0]) == {"state": "closed", "state_reason": "not_planned"}


def test_add_and_remove_labels() -> None:
    api = Api().on("POST", "issues/5/labels", [{"name": "claimed"}])
    assert make(api).add_labels(5, ["Claimed"]) == ("claimed",)
    with pytest.raises(ForgeValidationError):
        make(Api().on("POST", "issues/5/labels", [])).add_labels(5, ["claimed"])
    api = Api().on("DELETE", "issues/5/labels/state:proposed", [])
    assert make(api).remove_label(5, "state:proposed") == ()
    assert api.requests[0].url.raw_path == b"/repos/lab/articulo/issues/5/labels/state%3Aproposed"
    api = Api().on("DELETE", "issues/5/labels/x", 404).on("GET", "issues/5", issue_json(5, labels=[{"name": "y"}]))
    assert make(api).remove_label(5, "x") == ("y",)  # idempotente (§4.2)
    api = Api().on("DELETE", "issues/5/labels/x", 404).on("GET", "issues/5", 404)
    with pytest.raises(ForgeNotFound):
        make(api).remove_label(5, "x")


def test_ensure_labels_creates_updates_and_spaces_writes() -> None:
    api = Api().on("GET", "labels/claimed", 404).on("POST", "labels", Reply(201, {"name": "claimed"}))
    api.on("GET", "labels/blocked", {"name": "blocked", "color": "000000", "description": ""})
    api.on("PATCH", "labels/blocked", {"name": "blocked"})
    api.on("GET", "labels/done", {"name": "done", "color": "ededed", "description": ""})
    adapter = make(api)
    actions = adapter.ensure_labels([Label("claimed", "FBCA04", "d"), Label("blocked", "B60205"), Label("done")])
    assert actions == {"claimed": "created", "blocked": "updated", "done": "unchanged"}
    assert api.body(api.calls("POST", "labels")[0]) == {"name": "claimed", "color": "fbca04", "description": "d"}
    assert api.body(api.calls("PATCH")[0])["color"] == "b60205"
    assert len(adapter.waits) == 1 and 0 < adapter.waits[0] <= 1.0  # type: ignore[attr-defined] # REQ-FORGE-50


def test_ensure_labels_422_on_create_rereads() -> None:
    api = Api().on("GET", "labels/claimed", 404, {"name": "claimed", "color": "ededed", "description": ""})
    api.on("POST", "labels", Reply(422, {"message": "already_exists"}))
    assert make(api).ensure_labels([Label("claimed")]) == {"claimed": "unchanged"}  # otro la creó antes


def test_list_issue_events_filters_labels() -> None:
    events = [
        {"id": 2, "event": "assigned", "actor": user(), "created_at": "2026-10-02T10:00:00Z"},
        {"id": 3, "event": "unlabeled", "label": {"name": "claimed"}, "actor": user(), "created_at": "2026-10-03T10:00:00Z"},
        {"id": 1, "event": "labeled", "label": {"name": "claimed"}, "actor": user(), "created_at": "2026-10-01T10:00:00Z"},
    ]
    got = make(Api().on("GET", "issues/5/events", events)).list_issue_events(5)
    assert [(e.id, e.event) for e in got] == [(1, "labeled"), (3, "unlabeled")]
    with pytest.raises(ForgeNotFound):
        make(Api().on("GET", "issues/5/events", 410)).list_issue_events(5)


def test_assignees_are_verified() -> None:
    api = Api().on("PATCH", "issues/5", issue_json(5, assignees=[user("Bruno")]))
    assert make(api).set_assignees(5, ["bruno"]).assignees == ("Bruno",)
    assert api.body(api.requests[0]) == {"assignees": ["bruno"]}
    with pytest.raises(ForgeValidationError):  # descartado en silencio (REQ-FORGE-11)
        make(Api().on("PATCH", "issues/5", issue_json(5))).set_assignees(5, ["bruno"])
    api = Api().on("POST", "issues/5/assignees", Reply(201, issue_json(5, assignees=[user("ana"), user("bruno")])))
    assert make(api).add_assignees(5, ["bruno"]).assignees == ("ana", "bruno")
    api = Api().on("DELETE", "issues/5/assignees", issue_json(5, assignees=[user("ana")]))
    assert make(api).remove_assignees(5, ["bruno"]).assignees == ("ana",)
    assert api.body(api.requests[0]) == {"assignees": ["bruno"]}


def _comment(cid: int, body: str = "c", login: str = "ana", created: str = "2026-10-08T12:00:00Z") -> dict[str, Any]:
    return {"id": cid, "user": user(login), "body": body, "created_at": created, "updated_at": created,
            "html_url": f"https://github.com/lab/articulo/issues/5#issuecomment-{cid}"}


def test_comments_create_recover_and_update() -> None:
    api = Api().on("POST", "issues/5/comments", Reply(201, _comment(1)))
    make(api).create_comment(5, "hola", op_id="claim-5")
    assert api.body(api.requests[0])["body"].endswith(f"<!-- {_meta.NAME}:op v1 id=claim-5 -->")
    marker = f"<!-- {_meta.NAME}:op v1 id=claim-5 -->"
    api = Api().on("POST", "issues/5/comments", 502)
    api.on("GET", "/user", user()).on("GET", "/user/repos", [{"id": 1}])
    api.on("GET", "issues/5/comments", [_comment(2, f"hola\n\n{marker}")])
    assert make(api).create_comment(5, "hola", op_id="claim-5").id == 2
    api = Api().on("PATCH", "issues/comments/2", _comment(2, "nuevo"))
    assert make(api).update_comment(2, "nuevo").body == "nuevo"


# ---------------------------------------------------------------------------
# Pull requests y revisiones
# ---------------------------------------------------------------------------


def test_get_pull_request_has_merged_by_and_lists_do_not() -> None:
    merged = pull_json(7, merged=True, merged_at="2026-10-06T10:00:00Z", merged_by=user("lead"))
    pr = make(Api().on("GET", "pulls/7", merged)).get_pull_request(7)
    assert pr is not None and (pr.merged, pr.merged_by, pr.same_repo) == (True, "lead", True)
    summary = pull_json(7, merged_at="2026-10-06T10:00:00Z", merged_by=user("lead"))
    pulls = make(Api().on("GET", f"commits/{SHA_A}/pulls", [summary])).pulls_for_commit(SHA_A)
    assert (pulls[0].merged, pulls[0].merged_by) == (True, None)  # CT-SEAL-01
    assert make(Api().on("GET", "pulls/8", 404)).get_pull_request(8) is None


def test_find_pull_request_ignores_forks() -> None:
    api = Api().on("GET", "pulls", [pull_json(3, fork=True), pull_json(4), pull_json(2)])
    pr = make(api).find_pull_request("task/1", state="open")
    assert pr is not None and pr.number == 4
    assert api.requests[0].url.params["head"] == "lab:task/1"
    assert make(Api().on("GET", "pulls", [pull_json(3, fork=True)])).find_pull_request("task/1") is None


def test_list_pull_requests_keeps_forks_marked() -> None:
    prs = make(Api().on("GET", "pulls", [pull_json(1), pull_json(2, fork=True)])).list_pull_requests(state="all")
    assert [(p.number, p.same_repo) for p in prs] == [(2, False), (1, True)]


def test_create_pull_request_and_422_cases() -> None:
    api = Api().on("POST", "pulls", Reply(201, pull_json(5)))
    assert make(api).create_pull_request(head="task/1", base="main", title="t", body="b").number == 5
    assert api.body(api.requests[0])["draft"] is False
    api = Api().on("POST", "pulls", 422).on("GET", "pulls", [pull_json(5)])
    with pytest.raises(PullRequestExists) as info:
        make(api).create_pull_request(head="task/1", base="main", title="t", body="b")
    assert info.value.existing.number == 5
    cmp = {"status": "identical", "ahead_by": 0, "behind_by": 0, "merge_base_commit": {"sha": SHA_A}, "files": []}
    api = Api().on("POST", "pulls", 422).on("GET", "pulls", []).on("GET", "compare/main...task/1", cmp)
    with pytest.raises(NoCommitsBetween):  # REQ-FORGE-36
        make(api).create_pull_request(head="task/1", base="main", title="t", body="b")
    api = Api().on("POST", "pulls", Reply(raise_=httpx2.ReadTimeout("lost"))).on("GET", "pulls", [pull_json(6)])
    assert make(api).create_pull_request(head="task/1", base="main", title="t", body="b").number == 6


def test_update_pull_request_has_no_draft_and_ready_is_reserved() -> None:
    api = Api().on("PATCH", "pulls/5", pull_json(5, title="nuevo"))
    make(api).update_pull_request(5, title="nuevo")
    assert api.body(api.requests[0]) == {"title": "nuevo"}
    with pytest.raises(ForgeUnsupported) as info:
        make(Api(), hint=lambda key, **p: f"{key} {p['url']}").mark_ready_for_review(5)
    assert info.value.hint == "mark_ready https://github.com/lab/articulo/pull/5"


@dataclass(frozen=True)
class Confirmed:
    operation: str
    confirmed_at: datetime


def _review(rid: int, state: str = "APPROVED", login: str = "lead", at: str | None = "2026-10-08T12:00:00Z") -> dict[str, Any]:
    return {"id": rid, "user": user(login, 3), "state": state, "commit_id": SHA_A, "submitted_at": at}


def test_submit_review_needs_a_recent_confirmation() -> None:
    api = Api().on("POST", "pulls/5/reviews", _review(1))
    adapter = make(api)
    for bad in (Confirmed("request-changes", NOW), Confirmed("approve", NOW - timedelta(seconds=121)), object()):
        with pytest.raises(ForgeValidationError):  # REQ-FORGE-10
            adapter.submit_review(5, event="APPROVE", body="ok", commit_sha=SHA_A, confirmation=bad)  # type: ignore[arg-type]
    assert api.requests == []
    review = adapter.submit_review(5, event="APPROVE", body="ok", commit_sha=SHA_A, confirmation=Confirmed("approve", NOW))
    assert (review.state, review.author_id) == ("APPROVED", 3)
    assert api.body(api.requests[0]) == {"commit_id": SHA_A, "body": "ok", "event": "APPROVE"}


def test_list_reviews_chronological_with_pending_last() -> None:
    items = [_review(3, "PENDING", at=None), _review(2, at="2026-10-08T11:00:00Z"), _review(1, at="2026-10-07T11:00:00Z")]
    assert [r.id for r in make(Api().on("GET", "pulls/5/reviews", items)).list_reviews(5)] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Checks y workflows (§4.6)
# ---------------------------------------------------------------------------


def _status(*statuses: dict[str, Any], state: str = "success") -> dict[str, Any]:
    return {"state": state, "statuses": list(statuses)}


def test_checks_with_check_runs_api() -> None:
    runs = {"check_runs": [{"name": "build", "status": "completed", "conclusion": "success", "html_url": "u"}]}
    api = Api().on("GET", f"commits/{SHA_A}/check-runs", runs).on("GET", f"commits/{SHA_A}/status", _status())
    checks = make(api, token="ghp_classic").get_checks(SHA_A)
    assert (checks.overall, [r.source for r in checks.runs]) == ("success", ["check_run"])


def test_checks_fine_grained_uses_workflow_runs() -> None:
    runs = {"workflow_runs": [run_json(1, name="ci", conclusion="failure"), run_json(2, name="ci", conclusion="success",
                                                                               created_at="2026-10-09T10:00:00Z")]}
    api = Api().on("GET", "actions/runs", runs)
    api.on("GET", f"commits/{SHA_A}/status", _status({"context": "lint", "state": "pending", "target_url": "t"}, state="pending"))
    checks = make(api).get_checks(SHA_A)
    assert [(r.name, r.source, r.conclusion) for r in checks.runs] == [("ci", "workflow_run", "success"), ("lint", "status", None)]
    assert checks.overall == "pending"
    assert not api.calls("GET", f"commits/{SHA_A}/check-runs")  # REQ-FORGE-34


def test_checks_fall_back_after_403_and_combined_failure() -> None:
    api = Api().on("GET", f"commits/{SHA_A}/check-runs", 403).on("GET", "actions/runs", {"workflow_runs": []})
    api.on("GET", f"commits/{SHA_A}/status", _status({"context": "ext", "state": "error", "target_url": None}, state="failure"))
    adapter = make(api, token="ghp_classic")
    checks = adapter.get_checks(SHA_A)
    assert checks.overall == "failure" and adapter._checks_api is False
    assert make(Api().on("GET", "actions/runs", {"workflow_runs": []}).on(
        "GET", f"commits/{SHA_A}/status", _status(state="pending"))).get_checks(SHA_A).overall == "none"


def test_successful_runs_and_last_run() -> None:
    rows = [run_json(1), run_json(3), run_json(2, event="pull_request_target"), run_json(4, conclusion="failure")]
    api = Api().on("GET", "actions/workflows/seal.yml/runs", {"workflow_runs": rows})
    runs = make(api).successful_runs(".github/workflows/seal.yml", "main", event="push", limit=5)
    assert [r.id for r in runs] == [3, 1]  # REQ-FORGE-69
    params = api.requests[0].url.params
    assert (params["branch"], params["event"], params["status"], params["per_page"]) == ("main", "push", "success", "5")
    with pytest.raises(ForgeValidationError):
        make(api).successful_runs("seal.yml", "main", event="push", limit=0)
    api = Api().on("GET", "actions/workflows/seal.yml/runs",
                   {"workflow_runs": [run_json(9, created_at="2026-10-08T03:00:00Z")]})
    run = make(api).last_successful_run("seal.yml", "main")
    assert run is not None and run.id == 9 and api.requests[0].url.params["per_page"] == "1"
    assert run.created_at == datetime(2026, 10, 8, 3, 0, tzinfo=timezone.utc)  # doctor, REQ-CI-31
    assert runs[0].created_at == datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc)  # el run 3 de run_json
    assert make(Api().on("GET", "actions/workflows/seal.yml/runs", {"workflow_runs": []})).last_successful_run(
        "seal.yml", "main") is None
    with pytest.raises(ForgeNotFound):
        make(Api().on("GET", "actions/workflows/x.yml/runs", 404)).last_successful_run("x.yml", "main")


def test_get_workflow_run() -> None:
    run = make(Api().on("GET", "actions/runs/42", run_json(42))).get_workflow_run(42)
    assert (run.id, run.actor, run.head_branch) == (42, "ana", "main")


# ---------------------------------------------------------------------------
# Escrituras de configuración y del CI
# ---------------------------------------------------------------------------


def test_upsert_check_run_updates_or_creates() -> None:
    api = Api().on("GET", f"commits/{SHA_A}/check-runs", {"check_runs": [{"id": 5, "name": "editor-review"}]})
    api.on("PATCH", "check-runs/5", {"id": 5})
    make(api, token="ghs_job").upsert_check_run(SHA_A, "editor-review", conclusion="success", summary="ok")
    sent = api.body(api.calls("PATCH")[0])
    assert (sent["status"], sent["conclusion"], sent["output"]) == ("completed", "success", {"title": "editor-review", "summary": "ok"})
    api = Api().on("GET", f"commits/{SHA_A}/check-runs", {"check_runs": []}).on("POST", "check-runs", Reply(201, {"id": 6}))
    make(api, token="ghs_job").upsert_check_run(SHA_A, "editor-review", conclusion="failure", summary="no")
    assert api.body(api.calls("POST")[0])["head_sha"] == SHA_A  # REQ-FORGE-70


# ---------------------------------------------------------------------------
# Sesión web (§8): git y gh
# ---------------------------------------------------------------------------


class Git:
    def __init__(self, *results: tuple[int, bytes, bytes]) -> None:
        self.results = list(results)
        self.calls: list[tuple[tuple[str, ...], bytes | None, Mapping[str, str] | None]] = []

    def __call__(
        self, args: Sequence[str], *, input: bytes | None = None, extra_env: Mapping[str, str] | None = None
    ) -> tuple[int, bytes, bytes]:
        self.calls.append((tuple(args), input, extra_env))
        return self.results.pop(0)


WEB = {"CLAUDE_CODE_REMOTE": "true"}


def test_web_claim_commit_and_push() -> None:
    api = Api().on("GET", "/user", user("ana", 7)).on("GET", "/user/repos", [{"id": 1}])
    api.on("GET", "git/ref/heads/task/1", ref_json("task/1", SHA_B))
    git = Git((0, f"{SHA_B}\n".encode(), b""), (0, b"To origin\n*\tSHA:refs/heads/task/1\t[new branch]\nDone\n", b""))
    adapter = make(api, env=WEB, run_git=git)
    assert adapter.create_empty_commit(SHA_A, "claim\n") == SHA_B
    args, stdin, env = git.calls[0]
    assert args == ("commit-tree", f"{SHA_A}^{{tree}}", "-p", SHA_A, "-F", "-") and stdin == b"claim\n"
    assert env is not None and env["GIT_AUTHOR_EMAIL"] == "7+ana@users.noreply.github.com"  # REQ-FORGE-67
    assert adapter.create_ref("task/1", SHA_B) == Ref("task/1", SHA_B)
    assert git.calls[1][0] == ("push", "--porcelain", "--force-with-lease=refs/heads/task/1:", "origin",
                               f"{SHA_B}:refs/heads/task/1")
    assert not api.calls("POST")  # el proxy bloquea POST git/commits y git/refs


def test_web_claim_lost_race() -> None:
    api = Api().on("GET", "git/ref/heads/task/1", ref_json("task/1", SHA_C))
    git = Git((1, b"!\tx:refs/heads/task/1\t[rejected] (stale info)\nDone\n", b""))
    with pytest.raises(RefAlreadyExists) as info:
        make(api, env=WEB, run_git=git).create_ref("task/1", SHA_B)
    assert info.value.existing.sha == SHA_C


def test_web_claim_rereads_a_pushed_ref_that_is_not_visible_yet() -> None:
    api = Api().on("GET", "git/ref/heads/task/1", 404, ref_json("task/1", SHA_B))
    git = Git((0, b"To origin\n*\tSHA:refs/heads/task/1\t[new branch]\nDone\n", b""))
    assert make(api, env=WEB, run_git=git).create_ref("task/1", SHA_B) == Ref("task/1", SHA_B)


def test_web_update_by_push_without_force() -> None:
    git = Git((0, b" \tx:refs/heads/task/1\tA..B\nDone\n", b""))
    assert make(Api(), env=WEB, run_git=git).update_ref("task/1", SHA_B) == Ref("task/1", SHA_B)
    assert "--force" not in " ".join(git.calls[0][0])  # REQ-FORGE-68
    api = Api().on("GET", "git/ref/heads/task/1", ref_json("task/1", SHA_C))
    git = Git((1, b"!\tx:refs/heads/task/1\t[rejected] (non-fast-forward)\nDone\n", b""))
    with pytest.raises(RefConflict):
        make(api, env=WEB, run_git=git).update_ref("task/1", SHA_B)


def test_write_mode_can_only_be_forced_outside_the_web() -> None:
    flag = {_meta.ENV_PREFIX + "FORGE_WRITE_MODE": "api"}
    assert make(Api(), env=flag).write_mode == "api"
    assert make(Api(), env={**WEB, **flag}).write_mode == "git"
    assert make(Api(), env={**WEB, **flag, _meta.ENV_PREFIX + "TESTING": "1"}).write_mode == "api"


class GhCli:
    """``gh`` falso: cada llamada toma la siguiente ``(stdout, returncode, stderr)``; la última se repite."""

    def __init__(self, stdout: bytes, returncode: int = 0, *more: tuple[bytes, int, bytes]) -> None:
        self.outputs = [(stdout, returncode, b""), *more]
        self.calls: list[tuple[tuple[str, ...], bytes | None]] = []
        self.envs: list[Mapping[str, str] | None] = []

    def __call__(
        self,
        args: Sequence[str],
        *,
        timeout: float,
        input: bytes | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((tuple(args), input))
        self.envs.append(extra_env)
        out, rc, err = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        return subprocess.CompletedProcess(list(args), rc, out, err)


def test_proxy_token_uses_the_gh_transport() -> None:
    body = json.dumps(ref_json("main", SHA_A)).encode()
    cli = GhCli(b"HTTP/2.0 200 OK\r\nContent-Type: application/json\r\nEtag: \"z\"\r\n\r\n" + body)
    adapter = make(Api(), token="proxy-injected", env={**WEB, _meta.ENV_PREFIX + "FORGE_TRANSPORT": "http"}, run_gh=cli)
    assert adapter.get_ref("main") == Ref("main", SHA_A)
    args = cli.calls[0][0]
    assert args[:4] == ("api", "--include", "--method", "GET") and args[-1] == "repos/lab/articulo/git/ref/heads/main"
    assert "-H" in args and "X-GitHub-Api-Version: 2026-03-10" in args
    assert not any(a.startswith("Authorization") for a in args)  # la credencial no va en los argumentos
    assert cli.envs[0] == {"GH_TOKEN": "proxy-injected"}
    assert "W_FORGE_CAPABILITY_UNKNOWN" in adapter.notices  # REQ-FORGE-55


def test_forced_gh_transport_uses_the_classified_token() -> None:
    # gh debe autenticar con el mismo token que el adaptador clasificó, no con el de su llavero
    body = json.dumps(ref_json("main", SHA_A)).encode()
    cli = GhCli(b"HTTP/2.0 200 OK\n\n" + body)
    make(Api(), env={_meta.ENV_PREFIX + "FORGE_TRANSPORT": "gh"}, run_gh=cli).get_ref("main")
    assert cli.envs[0] == {"GH_TOKEN": FG}
    assert all(FG not in a for a in cli.calls[0][0])


def test_gh_write_without_response_is_ambiguous() -> None:
    # Sin línea de estado tras enviar un POST: resultado ambiguo, se confirma con un GET (§4.4)
    found = json.dumps(ref_json("task/1", SHA_A)).encode()
    cli = GhCli(b"", 1, (b"HTTP/2.0 200 OK\n\n" + found, 0, b""))
    cli.outputs[0] = (b"", 1, b"unexpected EOF")
    assert make(Api(), token="proxy-injected", run_gh=cli).create_ref("task/1", SHA_A) == Ref("task/1", SHA_A)
    assert [c[0][3] for c in cli.calls] == ["POST", "GET"]
    cli = GhCli(b"", 1)
    cli.outputs[0] = (b"", 1, b"dial tcp: lookup api.github.com: no such host")
    with pytest.raises(NetworkUnreachable):  # no llegó a conectar: la petición no salió
        make(Api(), token="proxy-injected", run_gh=cli).create_ref("task/1", SHA_A)


def test_gh_transport_errors_and_body() -> None:
    cli = GhCli(b"HTTP/2.0 404 Not Found\nContent-Type: application/json\n\n{\"message\":\"Not Found\"}", returncode=1)
    assert make(Api(), token="proxy-injected", run_gh=cli).get_ref("main") is None
    cli = GhCli(b"", returncode=1)
    with pytest.raises(NetworkUnreachable):
        make(Api(), token="proxy-injected", run_gh=cli).get_ref("main")
    created = json.dumps(ref_json("task/1", SHA_A)).encode()
    cli = GhCli(b"HTTP/2.0 201 Created\n\n" + created)
    make(Api(), token="proxy-injected", run_gh=cli).create_ref("task/1", SHA_A)
    args, stdin = cli.calls[0]
    assert "--input" in args and json.loads(stdin or b"") == {"ref": "refs/heads/task/1", "sha": SHA_A}


# ---------------------------------------------------------------------------
# Registro y eventos (REQ-FORGE-24, REQ-ARCH-90)
# ---------------------------------------------------------------------------


def test_errors_are_logged_without_credentials(caplog: pytest.LogCaptureFixture) -> None:
    api = Api().on("GET", "git/ref/heads/main", Reply(503, {"message": "x"}, {"X-GitHub-Request-Id": "AB:12"}))
    events: list[dict[str, Any]] = []
    adapter = make(api, on_event=lambda **f: events.append(f))
    with caplog.at_level(logging.DEBUG, logger=gh.__name__), pytest.raises(ForgeUnavailable):
        adapter.get_ref("main")
    text = caplog.text
    assert "GET /repos/lab/articulo/git/ref/heads/main -> 503 (request id AB:12)" in text
    assert FG not in text and "Bearer" not in text
    assert events[-1]["kind"] == "net" and events[-1]["net"] == {"host": "api.github.com", "status": 503}
    assert "path" not in events[-1]


# ---------------------------------------------------------------------------
# Correcciones de la revisión adversarial
# ---------------------------------------------------------------------------


def _stale_cache(tmp_path: Path) -> None:
    api = _caps_api(_repo()).on("GET", "rules/branches/main", RULES)
    make(api, cache=Store(tmp_path)).capabilities()


def _later(api: Api, tmp_path: Path, **kw: Any) -> GitHubAdapter:
    return GitHubAdapter(
        REPO, token=FG, base_branch="main", ci=False, cache=Store(tmp_path),
        http_transport=httpx2.MockTransport(api), now=lambda: NOW + timedelta(minutes=11), **kw,
    )


def test_revalidation_with_revoked_token_is_an_auth_error(tmp_path: Path) -> None:
    _stale_cache(tmp_path)
    api = Api().on("GET", "", Reply(401, {"message": "Bad credentials"}))
    with pytest.raises(ForgeAuthError):  # REQ-FORGE-19: no devuelve las capacidades viejas
        _later(api, tmp_path).capabilities()


def test_revalidation_404_redetects_and_fails(tmp_path: Path) -> None:
    _stale_cache(tmp_path)
    api = Api().on("GET", "", 404)
    with pytest.raises(ForgeNotFound):
        _later(api, tmp_path).capabilities()
    assert len(api.calls("GET", "")) == 2  # la revalidación y la redetección


def test_profile_warning_only_when_it_becomes_advisory(tmp_path: Path) -> None:
    _stale_cache(tmp_path)
    api = Api().on("GET", "", _repo(admin=True)).on("GET", "rules/branches/main", RULES)
    adapter = _later(api, tmp_path)
    assert adapter.capabilities().profile == "enforced"
    assert "W_FORGE_PROFILE_ADVISORY" not in adapter.notices  # el visor solo ganó permisos
    other = tmp_path / "otro"
    other.mkdir()
    _stale_cache(other)
    api = Api().on("GET", "", _repo("private")).on("GET", "/orgs/lab", {"login": "lab", "plan": {"name": "free"}})
    adapter = _later(api, other)
    assert adapter.capabilities().profile == "advisory"
    assert "W_FORGE_PROFILE_ADVISORY" in adapter.notices


def test_304_keeps_the_oauth_scopes(tmp_path: Path) -> None:
    api = Api().on(
        "GET",
        "/user",
        Reply(200, user(), {"ETag": '"u1"', "X-OAuth-Scopes": "repo, workflow"}),
        Reply(304, None, {"ETag": '"u1"'}),
    )
    make(api, token="ghp_classic", cache=Store(tmp_path)).identity()
    ident = make(api, token="ghp_classic", cache=Store(tmp_path)).identity(refresh=True)
    assert api.requests[1].headers["If-None-Match"] == '"u1"'
    assert (ident.token_scope, ident.oauth_scopes) == ("broad", ("repo", "workflow"))  # REQ-FORGE-39 y 40


def test_remove_label_404_still_checks_the_effect() -> None:
    api = Api().on("DELETE", "issues/5/labels/x", 404).on("GET", "issues/5", issue_json(5, labels=[{"name": "x"}]))
    with pytest.raises(ForgeValidationError):  # REQ-FORGE-11
        make(api).remove_label(5, "x")


def test_create_pull_request_422_with_failing_probe_is_validation() -> None:
    api = Api().on("POST", "pulls", 422).on("GET", "pulls", []).on("GET", "compare/main...task/9", 404)
    with pytest.raises(ForgeValidationError) as info:  # REQ-FORGE-36: «en cualquier otro caso»
        make(api).create_pull_request(head="task/9", base="main", title="t", body="b")
    assert type(info.value) is ForgeValidationError


def test_ensure_labels_422_is_resolved_per_label() -> None:
    api = Api().on("GET", "labels/a", 404, {"name": "a", "color": "ededed", "description": ""})
    api.on("GET", "labels/b", 404)
    api.on("POST", "labels", Reply(422, {"message": "already_exists"}), Reply(201, {"name": "b"}))
    make(api).ensure_labels([Label("a"), Label("b")])
    assert [api.body(r)["name"] for r in api.calls("POST", "labels")] == ["a", "b"]


def test_combined_status_is_paginated_and_pending_counts() -> None:
    nxt = f"https://api.github.com/repos/lab/articulo/commits/{SHA_A}/status?page=2"
    first = Reply(200, {"state": "pending", "statuses": [{"context": f"c{i}", "state": "success"} for i in range(3)]},
                  {"Link": f'<{nxt}>; rel="next"'})
    second = Reply(200, {"state": "pending", "statuses": [{"context": "late", "state": "pending"}]})
    api = Api().on("GET", "actions/runs", {"workflow_runs": []}).on("GET", f"commits/{SHA_A}/status", first, second)
    checks = make(api).get_checks(SHA_A)
    assert checks.overall == "pending" and len(checks.runs) == 4
    assert api.calls("GET", f"commits/{SHA_A}/status")[0].url.params["per_page"] == "100"


def test_check_runs_are_sorted_by_name() -> None:
    runs = {"check_runs": [{"id": 2, "name": "z", "status": "completed", "conclusion": "success"},
                           {"id": 1, "name": "a", "status": "completed", "conclusion": "success"}]}
    api = Api().on("GET", f"commits/{SHA_A}/check-runs", runs).on("GET", f"commits/{SHA_A}/status", _status())
    assert [r.name for r in make(api, token="ghp_classic").get_checks(SHA_A).runs] == ["a", "z"]


def test_commit_files_409_on_the_ref_is_unavailable() -> None:
    api = Api().on("GET", f"git/commits/{SHA_A}", {"sha": SHA_A, "tree": {"sha": "t" * 40}})
    api.on("POST", "git/trees", Reply(201, {"sha": "e" * 40})).on("POST", "git/commits", Reply(201, {"sha": SHA_B}))
    api.on("PATCH", "git/refs/heads/task/1", 409)
    with pytest.raises(ForgeUnavailable):
        make(api).commit_files("task/1", expected_head=SHA_A, changes=[FileChange("a", b"x")], message="m")


def test_delete_ref_with_lost_response() -> None:
    api = Api().on("DELETE", "git/refs/heads/task/1", 502).on("GET", "git/ref/heads/task/1", 404)
    assert make(api).delete_ref("task/1") is True  # el borrado se aplicó (CT-REF-04)
    assert len(api.calls("DELETE")) == 1
    api = Api().on("DELETE", "git/refs/heads/task/1", 502).on("GET", "git/ref/heads/task/1", ref_json("task/1", SHA_A))
    with pytest.raises(ForgeUnavailable):
        make(api).delete_ref("task/1")


def test_unauthorized_emits_a_net_event() -> None:
    events: list[dict[str, Any]] = []
    api = Api().on("GET", "git/ref/heads/main", Reply(401, {"message": "Bad credentials"}))
    with pytest.raises(ForgeAuthError):
        make(api, on_event=lambda **f: events.append(f)).get_ref("main")
    assert events and events[-1]["net"] == {"host": "api.github.com", "status": 401}


# ---------------------------------------------------------------------------
# Fábrica
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "lab", "lab/a/b", "-lab/x", "lab/..", "la b/x"])
def test_parse_repo_rejects_invalid(value: object) -> None:
    with pytest.raises(ForgeValidationError):
        parse_repo(value)


@dataclass
class _Inv:
    env: Mapping[str, str]
    surface: str = "cli"
    now: Callable[[], datetime] = lambda: NOW


class _Ctx:
    def __init__(self, repo: str | None, surface: str = "cli", env: Mapping[str, str] | None = None) -> None:
        self.inv = _Inv(env or {}, surface)
        self._repo = repo

    def forge_target(self) -> Any:
        return type("T", (), {"type": "github", "repo": self._repo})()

    def base(self) -> Any:
        raise RuntimeError("no project")

    def project(self, *, required: bool = True) -> None:
        return None

    def t(self) -> Any:
        raise RuntimeError("no translator")


def test_create_from_context() -> None:
    adapter = create(_Ctx("lab/articulo"))
    assert adapter.repo == REPO and adapter._ci is False and adapter._deadline is not None
    assert adapter._caches.backend is None  # sin proyecto, en memoria
    ci = create(_Ctx("lab/articulo", surface="ci"))
    assert ci._ci is True and ci._deadline is None  # REQ-FORGE-18: sin caché en disco ni tope
    with pytest.raises(ForgeValidationError):
        create(_Ctx(None))


# ---------------------------------------------------------------------------
# Administración (08 §2.2; 00-marco D-62): solo init
# ---------------------------------------------------------------------------

ADMIN = "github_pat_ADMINTOKEN"


def make_admin(api: Api, *, token: str = ADMIN, **kw: Any) -> gh.GitHubAdmin:
    waits: list[float] = []
    kw.setdefault("env", {})
    admin = gh.GitHubAdmin(
        REPO, token, http_transport=httpx2.MockTransport(api), now=lambda: NOW, sleep=waits.append,
        jitter=lambda: 0.5, **kw,
    )
    admin.waits = waits  # type: ignore[attr-defined]
    return admin


def repo_json(**over: Any) -> dict[str, Any]:
    d = {"visibility": "private", "default_branch": "main", "owner": user("lab", 3, "Organization"),
         "permissions": {"admin": True}, "allow_squash_merge": True, "allow_merge_commit": True,
         "allow_rebase_merge": True, "squash_merge_commit_title": "COMMIT_OR_PR_TITLE",
         "squash_merge_commit_message": "COMMIT_MESSAGES", "allow_auto_merge": False, "allow_update_branch": False,
         "delete_branch_on_merge": False, "allow_forking": True, "has_wiki": True, "name": "articulo"}
    d.update(over)
    return d


def test_admin_uses_its_own_token_and_no_disk_cache(tmp_path: Path) -> None:
    """REQ-FORGE-71: el token llega explícito (no se resuelve del entorno) y no hay caché en disco."""
    api = Api().on("GET", "/user", user()).on("GET", "", repo_json())
    admin = make_admin(api, env={"GH_TOKEN": "gho_OTHER", _meta.ENV_PREFIX + "GITHUB_TOKEN": "github_pat_OTHER"})
    assert admin.identity().login == "ana" and admin.identity().login == "ana"
    assert admin.repo_settings()["allow_merge_commit"] is True
    assert {r.headers["authorization"] for r in api.requests} == {f"Bearer {ADMIN}"}
    assert len(api.calls("GET", "/user")) == 1
    with pytest.raises(TypeError):
        make_admin(api, cache=Store(tmp_path))
    with pytest.raises(NoTokenError):
        gh.GitHubAdmin(REPO, "  ")


def test_admin_repo_settings() -> None:
    changed = repo_json(allow_merge_commit=False, squash_merge_commit_title="PR_TITLE",
                        squash_merge_commit_message="PR_BODY")
    api = Api().on("GET", "", repo_json(private=True)).on("PATCH", "", changed)
    admin = make_admin(api)
    settings = admin.repo_settings()
    assert set(settings) == set(gh.REPO_SETTINGS) and "visibility" not in settings
    after = admin.update_repo_settings({"allow_merge_commit": False, "squash_merge_commit_title": "PR_TITLE",
                                        "squash_merge_commit_message": "PR_BODY"})
    assert after["squash_merge_commit_message"] == "PR_BODY"
    assert api.body(api.calls("PATCH")[0]) == {"allow_merge_commit": False, "squash_merge_commit_title": "PR_TITLE",
                                               "squash_merge_commit_message": "PR_BODY"}
    with pytest.raises(ForgeValidationError):
        admin.update_repo_settings({"visibility": "public"})  # REQ-FORGE-72
    assert len(api.calls("PATCH")) == 1
    ignored = Api().on("PATCH", "", repo_json())  # la forja no aplicó el cambio (REQ-FORGE-11)
    with pytest.raises(ForgeValidationError):
        make_admin(ignored).update_repo_settings({"has_wiki": False})


def test_admin_actions_policy() -> None:
    api = Api().on("GET", "", repo_json())
    api.on("GET", "actions/permissions", {"enabled": True, "allowed_actions": "selected", "sha_pinning_required": False})
    api.on("GET", "actions/permissions/selected-actions",
           {"github_owned_allowed": True, "verified_allowed": False, "patterns_allowed": ["astral-sh/setup-uv@*"]})
    api.on("GET", "/orgs/lab/actions/permissions", 403)
    api.on("PUT", "actions/permissions", Reply(204))
    admin = make_admin(api)
    policy = admin.actions_policy()
    assert policy is not None and policy.patterns_allowed == ("astral-sh/setup-uv@*",) and policy.github_owned_allowed
    assert admin.actions_policy("org") is None  # sin permiso de owner: desconocida (REQ-PERM-56)
    admin.set_actions_policy(enabled=True, allowed_actions="selected", sha_pinning_required=True)
    assert api.body(api.calls("PUT")[0]) == {"enabled": True, "allowed_actions": "selected", "sha_pinning_required": True}
    personal = Api().on("GET", "", repo_json(owner=user("ana")))
    assert make_admin(personal).actions_policy("org") is None and len(personal.requests) == 1


def test_admin_workflow_permissions_and_retention() -> None:
    api = Api().on("GET", "actions/permissions/workflow",
                   {"default_workflow_permissions": "write", "can_approve_pull_request_reviews": True})
    api.on("PUT", "actions/permissions/workflow", Reply(204))
    api.on("GET", "actions/permissions/artifact-and-log-retention", {"days": 90, "maximum_allowed_days": 400})
    api.on("PUT", "actions/permissions/artifact-and-log-retention", Reply(204))
    admin = make_admin(api)
    assert admin.workflow_permissions() == gh.WorkflowPermissions("write", True)
    admin.set_workflow_permissions(default="read", can_approve=False)
    assert api.body(api.calls("PUT", "actions/permissions/workflow")[0]) == {
        "default_workflow_permissions": "read", "can_approve_pull_request_reviews": False}
    assert admin.retention_days() == 90
    admin.set_retention_days(400)
    missing = Api().on("GET", "actions/permissions/artifact-and-log-retention", 404)
    missing.on("PUT", "actions/permissions/artifact-and-log-retention", 404)
    assert make_admin(missing).retention_days() is None  # REQ-PERM-57: endpoint por confirmar en S5
    with pytest.raises(ForgeUnsupported):
        make_admin(missing).set_retention_days(400)


def test_admin_collaborators_and_users() -> None:
    api = Api().on("GET", "collaborators/bruno/permission", {"permission": "write", "role_name": "maintain"})
    api.on("GET", "collaborators/dario/permission", {"permission": "none", "role_name": "read"})
    api.on("GET", "collaborators/eva/permission", 404)
    api.on("PUT", "collaborators/carla", Reply(201, {"id": 1}))
    api.on("PUT", "collaborators/bruno", Reply(204))
    api.on("GET", "collaborators", [user("fede", 9)])
    api.on("GET", "/users/carla", user("carla", 42))
    admin = make_admin(api)
    assert admin.collaborator_permission("bruno") == "maintain"
    assert admin.collaborator_permission("dario") is None and admin.collaborator_permission("eva") is None
    assert admin.add_collaborator("carla", "push") == "invited" and admin.add_collaborator("bruno", "push") == "added"
    assert api.body(api.calls("PUT", "collaborators/carla")[0]) == {"permission": "push"}
    assert admin.outside_collaborators() == ["fede"]
    assert api.calls("GET", "collaborators")[0].url.params["affiliation"] == "outside"
    assert admin.user_id("carla") == 42


def test_admin_org_settings() -> None:
    api = Api().on("GET", "", repo_json()).on("GET", "/orgs/lab", {
        "login": "lab", "plan": {"name": "team"}, "default_repository_permission": "none",
        "members_can_fork_private_repositories": False, "two_factor_requirement_enabled": True})
    assert make_admin(api).org_settings() == {"default_repository_permission": "none",
                                               "members_can_fork_private_repositories": False,
                                               "two_factor_requirement_enabled": True}
    hidden = Api().on("GET", "", repo_json()).on("GET", "/orgs/lab", 404)
    assert make_admin(hidden).org_settings() is None


def test_admin_rulesets() -> None:
    api = Api().on("GET", "rulesets", [{"id": 4, "name": "aucove-base"}])
    api.on("GET", "rulesets/4", {"id": 4, "name": "aucove-base", "target": "branch", "enforcement": "active",
                                 "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
                                 "rules": [{"type": "deletion"}], "bypass_actors": [], "_links": {}})
    api.on("PUT", "rulesets/4", {"id": 4})
    api.on("GET", "rules/branches/main", [{"type": "deletion", "ruleset_id": 4}])
    admin = make_admin(api)
    info = admin.ruleset("aucove-base")
    assert info is not None and info.id == 4 and info.payload["rules"] == [{"type": "deletion"}]
    assert "_links" not in info.payload
    assert admin.ruleset("other") is None
    assert admin.upsert_ruleset("aucove-base", {"target": "branch"}) == 4
    assert admin.branch_rules("main") == [{"type": "deletion", "ruleset_id": 4}]
    created = Api().on("GET", "rulesets", []).on("POST", "rulesets", Reply(201, {"id": 11}))
    assert make_admin(created).upsert_ruleset("aucove-base", {"target": "branch"}) == 11
    assert created.body(created.calls("POST")[0]) == {"target": "branch", "name": "aucove-base"}
    assert created.calls("GET")[0].url.params["includes_parents"] == "false"
    plan = Api().on("GET", "rulesets", []).on("POST", "rulesets", Reply(403, {"message": "Upgrade to GitHub Pro"}))
    with pytest.raises(ForgeUnsupported):
        make_admin(plan).upsert_ruleset("x", {})
    denied = Api().on("GET", "rulesets", Reply(403, {"message": "Resource not accessible by personal access token"}))
    with pytest.raises(ForgePermissionError):
        make_admin(denied).upsert_ruleset("x", {})


# ---------------------------------------------------------------------------
# Lecturas de la configuración con el token de la persona (00-marco D-65; REQ-FORGE-73)
# ---------------------------------------------------------------------------


def test_repo_settings_reuse_the_detection_of_this_process() -> None:
    """doctor detecta y luego lee los ajustes: un solo GET /repos (08 §7.5)."""
    api = _caps_api(repo_json(visibility="public")).on("GET", "rules/branches/main", RULES)
    adapter = make(api)
    adapter.capabilities(refresh=True)
    settings = adapter.repo_settings()
    assert settings["allow_merge_commit"] is True and set(settings) <= set(gh.REPO_SETTINGS)
    assert len(api.calls("GET", "")) == 1
    reader = Api().on("GET", "", {k: v for k, v in repo_json().items() if k not in ("allow_squash_merge", "has_wiki")})
    assert "allow_squash_merge" not in make(reader).repo_settings()  # sin escritura no vienen


def test_rulesets_labels_and_collaborators() -> None:
    api = Api().on("GET", "rulesets", [{"id": 4, "name": "aucove-base"}, {"id": 5, "name": "otro"}])
    api.on("GET", "rulesets/4", {"id": 4, "name": "aucove-base", "target": "branch", "enforcement": "active",
                                 "rules": [{"type": "deletion"}]})  # sin bypass_actors: no puede editarlo
    api.on("GET", "rulesets/5", {"id": 5, "name": "otro", "target": "push", "enforcement": "evaluate", "rules": []})
    api.on("GET", "labels", [{"name": "task", "color": "1d76db", "description": "Tarea"}, {"color": "x"}])
    api.on("GET", "collaborators/bruno/permission", {"permission": "write", "role_name": "write"})
    api.on("GET", "collaborators", [user("fede", 9)])
    adapter = make(api)
    rulesets = adapter.rulesets()
    assert [(r.id, r.name, r.enforcement) for r in rulesets] == [(4, "aucove-base", "active"), (5, "otro", "evaluate")]
    assert "bypass_actors" not in rulesets[0].payload
    assert api.calls("GET", "rulesets")[0].url.params["includes_parents"] == "false"
    assert adapter.list_labels() == [gh.Label("task", "1d76db", "Tarea")]
    assert adapter.collaborator_permission("bruno") == "write"
    assert adapter.outside_collaborators() == ["fede"]
    free = Api().on("GET", "rulesets", Reply(403, {"message": "Upgrade to GitHub Pro or make this repository public"}))
    with pytest.raises(ForgeUnsupported):
        make(free).rulesets()


def test_reads_reserved_to_administration_are_unknown() -> None:
    """Con el token de uso diario (sin Administration): None, sin fallar (REQ-PERM-56; REQ-OPS-64)."""
    api = Api().on("GET", "", repo_json())
    for path in ("actions/permissions", "/orgs/lab/actions/permissions", "actions/permissions/workflow",
                 "/orgs/lab/actions/permissions/workflow"):
        api.on("GET", path, Reply(403, {"message": "Resource not accessible by personal access token"}))
    api.on("GET", "/orgs/lab", {"login": "lab", "plan": None})
    adapter = make(api)
    assert adapter.actions_policy() is None and adapter.actions_policy("org") is None
    assert adapter.workflow_permissions() is None and adapter.workflow_permissions("org") is None
    assert adapter.org_settings() == {}  # visible, pero sin los campos que solo ven los owners
    assert len(api.calls("GET", "")) == 1  # el dueño sale de la misma respuesta de GET /repos


def test_create_admin_takes_the_repository_of_the_context() -> None:
    class Ctx:
        inv = SimpleNamespace(env={}, now=lambda: NOW)

        def forge_target(self) -> SimpleNamespace:
            return SimpleNamespace(type="github", repo="lab/articulo")

    admin = gh.create_admin(Ctx(), ADMIN)
    assert isinstance(admin, gh.GitHubAdmin) and admin.repo == REPO
    admin.close()
