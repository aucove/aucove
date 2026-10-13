# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""CT-REF-01..04: refs y su creación concurrente (08-forja §4.4, §10.3; REQ-FORGE-28..31)."""

from __future__ import annotations

import threading

import pytest

from aucove.forge.base import Ref, RefAlreadyExists, RefConflict
from tests.contract.conftest import Sandbox


def test_ct_ref_01_create_new_ref(sandbox: Sandbox) -> None:
    """CT-REF-01: crear una ref nueva devuelve la Ref con el sha pedido."""
    branch = sandbox.branch("ref-01")
    sha = sandbox.claim_commit()
    ref = sandbox.forge.create_ref(branch, sha)
    assert ref == Ref(branch, sha)
    assert sandbox.forge.get_ref(branch) == Ref(branch, sha)
    assert sandbox.forge.delete_ref(branch) is True


def test_ct_ref_02_create_existing_ref(sandbox: Sandbox) -> None:
    """CT-REF-02: una ref existente con otro sha da RefAlreadyExists con el sha existente."""
    branch = sandbox.branch("ref-02")
    first = sandbox.claim_commit()
    second = sandbox.claim_commit()
    assert first != second
    sandbox.forge.create_ref(branch, first)
    with pytest.raises(RefAlreadyExists) as info:
        sandbox.forge.create_ref(branch, second)
    exc = info.value
    assert exc.existing == Ref(branch, first)
    assert exc.kind == "ref_exists" and exc.retryable is False
    # Estado y mensaje reales de la forja (08 §4.4: 409 o 422, sin depender de ellos).
    assert exc.http_status in (409, 422)
    assert exc.message
    # Con el sha pedido es éxito (REQ-FORGE-28).
    assert sandbox.forge.create_ref(branch, first) == Ref(branch, first)
    assert sandbox.forge.delete_ref(branch) is True


def test_ct_ref_03_concurrent_creation_has_one_winner(sandbox: Sandbox) -> None:
    """CT-REF-03: 10 hilos, 10 commits de reclamo distintos y la misma ref: 1 éxito y 9 RefAlreadyExists."""
    branch = sandbox.branch("ref-03")
    shas = [sandbox.claim_commit(message=f"claim race {i}") for i in range(10)]
    assert len(set(shas)) == 10
    barrier = threading.Barrier(10)
    results: list[Ref | BaseException] = [RuntimeError("not run")] * 10

    def worker(i: int) -> None:
        barrier.wait()
        try:
            results[i] = sandbox.forge.create_ref(branch, shas[i])
        except BaseException as exc:  # se examinan abajo
            results[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)

    wins = [r for r in results if isinstance(r, Ref)]
    losses = [r for r in results if isinstance(r, RefAlreadyExists)]
    assert len(wins) == 1, results
    assert len(losses) == 9, results
    winner = wins[0]
    assert winner.sha in shas
    assert all(loss.existing == winner for loss in losses)
    # La ref, leída dos veces, apunta al commit del ganador.
    assert sandbox.forge.get_ref(branch) == winner
    assert sandbox.forge.get_ref(branch) == winner
    assert sandbox.forge.delete_ref(branch) is True


def test_ct_ref_04_update_and_delete(sandbox: Sandbox) -> None:
    """CT-REF-04: sin avance rápido, RefConflict; con avance rápido, éxito; borrar dos veces, True y False."""
    branch = sandbox.branch("ref-04")
    c1 = sandbox.claim_commit()
    sibling = sandbox.claim_commit()  # hijo de la base, no desciende de c1
    sandbox.forge.create_ref(branch, c1)

    with pytest.raises(RefConflict) as info:
        sandbox.forge.update_ref(branch, sibling)
    assert info.value.current == c1
    assert sandbox.forge.get_ref(branch) == Ref(branch, c1)

    c2 = sandbox.claim_commit(parent=c1)
    assert sandbox.forge.update_ref(branch, c2) == Ref(branch, c2)
    assert sandbox.forge.get_ref(branch) == Ref(branch, c2)

    assert sandbox.forge.delete_ref(branch) is True
    assert sandbox.forge.delete_ref(branch) is False
    assert sandbox.forge.get_ref(branch) is None


def test_get_ref_missing_is_none(sandbox: Sandbox) -> None:
    assert sandbox.forge.get_ref(sandbox.branch("never-created")) is None


def test_list_refs_by_prefix(sandbox: Sandbox) -> None:
    prefix = sandbox.branch("list/")
    created = []
    for name in ("b", "a", "c"):
        sha = sandbox.claim_commit()
        created.append(sandbox.forge.create_ref(prefix + name, sha))
    listed = sandbox.forge.list_refs(prefix)
    assert listed == sorted(created, key=lambda r: r.branch)
    assert sandbox.forge.list_refs(sandbox.branch("nothing-here/")) == []
    for ref in created:
        assert sandbox.forge.delete_ref(ref.branch) is True


def test_update_to_missing_ref_is_conflict(sandbox: Sandbox) -> None:
    with pytest.raises(RefConflict) as info:
        sandbox.forge.update_ref(sandbox.branch("missing"), sandbox.claim_commit())
    assert info.value.current is None


def test_tag_namespace(sandbox: Sandbox) -> None:
    """refs/tags/archive/task-<n>-<k> (04 §4.1): mismo contrato en el espacio de etiquetas."""
    tag = sandbox.branch("archive/task-1-1")
    sha = sandbox.claim_commit()
    assert sandbox.forge.create_ref(tag, sha, namespace="tags") == Ref(tag, sha)
    assert sandbox.forge.get_ref(tag, namespace="tags") == Ref(tag, sha)
    assert sandbox.forge.get_ref(tag) is None  # no es una rama
    assert sandbox.forge.delete_ref(tag, namespace="tags") is True
    assert sandbox.forge.delete_ref(tag, namespace="tags") is False


# --- Solo con el falso: detalles de la simulación -------------------------------


def test_fake_rejects_unknown_sha_and_invalid_name(fake_sandbox: Sandbox) -> None:
    from aucove.forge.base import ForgeValidationError

    forge = fake_sandbox.forge
    with pytest.raises(ForgeValidationError):
        forge.create_ref("task/9", "f" * 40)
    with pytest.raises(ForgeValidationError):
        forge.create_ref("bad..name", fake_sandbox.base_sha)
    assert forge.get_ref("task/9") is None


def test_fake_create_ref_records_post_and_confirmation_get(fake_sandbox: Sandbox) -> None:
    fake = fake_sandbox.fake
    assert fake is not None
    sha = fake_sandbox.claim_commit()
    fake.calls.clear()
    fake.create_ref("task/1", sha)
    assert fake.calls.count("E", "create_ref") == 1 and fake.calls.count("L") == 0
    with pytest.raises(RefAlreadyExists):
        fake.create_ref("task/1", fake_sandbox.claim_commit())
    # El 422 se confirma con un GET, sin reintentar el POST (REQ-FORGE-29).
    assert fake.calls.count("E", "create_ref") == 2
    assert fake.calls.count("L", "create_ref", http="GET") == 1
