# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Token de la forja GitHub (08-forja §5; ``forge/auth.py``)."""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Sequence

import pytest

from aucove import _meta
from aucove.forge.auth import (
    GH_AUTH_ARGS,
    GH_TIMEOUT_S,
    PROXY_TOKEN,
    Token,
    classify_token,
    is_ci_token,
    make_token,
    resolve_token,
    token_fp,
)
from aucove.forge.base import ForgeAuthError, NoTokenError

OWN = _meta.ENV_PREFIX + "GITHUB_TOKEN"


class Gh:
    """``gh`` falso: devuelve ``stdout`` con ``returncode`` o lanza ``exc``."""

    def __init__(self, stdout: bytes = b"", returncode: int = 0, exc: BaseException | None = None) -> None:
        self.stdout, self.returncode, self.exc = stdout, returncode, exc
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def __call__(
        self, args: Sequence[str], *, timeout: float, input: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((tuple(args), timeout))
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(list(args), self.returncode, self.stdout, b"")


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("github_pat_11ABC", "fine_grained_pat"),
        ("ghp_abc", "classic_pat"),
        ("gho_abc", "oauth"),
        ("ghu_abc", "app_user"),
        ("ghs_abc", "app_installation"),
        (PROXY_TOKEN, "proxy"),
        ("x-unknown", "unknown"),
    ],
)
def test_classify_by_prefix(value: str, kind: str) -> None:
    # 08 §5.2
    assert classify_token(value) == kind


def test_fingerprint_is_sha256_prefix() -> None:
    # REQ-FORGE-6
    assert token_fp("ghp_secret") == hashlib.sha256(b"ghp_secret").hexdigest()[:12]


def test_token_value_never_in_repr() -> None:
    token = make_token("ghp_supersecret", "GH_TOKEN")
    assert "supersecret" not in repr(token)
    assert token.fp == token_fp("ghp_supersecret") and token.kind == "classic_pat"


@pytest.mark.parametrize("value", ["ghp_a b", "ghp_a\nb", "ghp_é", ""])
def test_invalid_characters_are_an_auth_error(value: str) -> None:
    with pytest.raises(ForgeAuthError) as info:
        make_token(value, "GH_TOKEN")
    assert value not in info.value.message or not value


def test_order_of_sources() -> None:
    # REQ-FORGE-37: variable propia, GH_TOKEN, GITHUB_TOKEN y gh auth token
    gh = Gh(b"gho_fromgh\n")
    env = {OWN: "ghp_own", "GH_TOKEN": "ghp_gh", "GITHUB_TOKEN": "ghs_actions"}
    assert resolve_token(env, gh).source == OWN
    del env[OWN]
    assert resolve_token(env, gh).source == "GH_TOKEN"
    del env["GH_TOKEN"]
    assert resolve_token(env, gh).source == "GITHUB_TOKEN"
    del env["GITHUB_TOKEN"]
    token = resolve_token(env, gh)
    assert (token.source, token.value, token.kind) == ("gh_auth_token", "gho_fromgh", "oauth")
    assert gh.calls == [(GH_AUTH_ARGS, GH_TIMEOUT_S)]  # sin shell y con 5 s (REQ-FORGE-38)


def test_blank_variables_are_skipped() -> None:
    assert resolve_token({OWN: "  ", "GH_TOKEN": " ghp_x "}, None).value == "ghp_x"


@pytest.mark.parametrize(
    "gh",
    [
        Gh(exc=FileNotFoundError("gh")),
        Gh(exc=subprocess.TimeoutExpired("gh", 5)),
        Gh(b"", returncode=1),
        Gh(b"\n"),
    ],
)
def test_gh_failure_is_no_token(gh: Gh) -> None:
    with pytest.raises(NoTokenError) as info:
        resolve_token({}, gh, hint="define GH_TOKEN")
    assert info.value.hint == "define GH_TOKEN"


def test_without_runner_gh_is_not_consulted() -> None:
    with pytest.raises(NoTokenError):
        resolve_token({}, None)


def test_ci_token_needs_actions_run_and_installation_token() -> None:
    # REQ-FORGE-43
    ghs = make_token("ghs_job", "GITHUB_TOKEN")
    env = {"GITHUB_ACTIONS": "true", "GITHUB_RUN_ID": "42"}
    assert is_ci_token(env, ghs)
    assert not is_ci_token({**env, "GITHUB_RUN_ID": ""}, ghs)
    assert not is_ci_token({"GITHUB_RUN_ID": "42"}, ghs)
    assert not is_ci_token(env, make_token("github_pat_x", "GH_TOKEN"))
    assert isinstance(ghs, Token)
