"""V3-007 — Unit of work, idempotency, inbox/outbox (design/09 §4, test-catalog T-003/004/099-106).

Each test names the catalog id it evidences. A test that only asserts the existing
behaviour is still evidence: the catalog marks every case ``specified_not_executed``
until a real run is tied to an implementation commit.
"""

from __future__ import annotations

import errno
import os
import sqlite3
from pathlib import Path

import pytest

from amplai_foundry.runtime.errors import Conflict, Hold, RuntimeFault
from amplai_foundry.runtime.evidence.cas import ArtifactStore
from amplai_foundry.runtime.recovery.service import OutboxPump, RecoveryService
from amplai_foundry.runtime.storage import store as store_module
from amplai_foundry.runtime.storage.store import Store

# ---------------------------------------------------------------- T-003 / T-004


def test_t003_idempotent_exact_repeat_returns_original_without_second_effect(deployment):
    s, scope = deployment.store, deployment.scope
    effects: list[int] = []

    def op(db):
        effects.append(1)
        s.put(db, scope, "record", "once", 1, {"v": 1})
        return {"created": "once"}

    first = s.command(scope, "actor", "k-lost-ack", {"x": 1}, op)
    second = s.command(scope, "actor", "k-lost-ack", {"x": 1}, op)
    assert first == second == {"created": "once"}
    assert effects == [1]
    assert len(s.list_objects(scope, "record")) == 1


def test_t004_same_key_different_payload_conflicts_and_state_unchanged(deployment):
    s, scope = deployment.store, deployment.scope

    def op(db):
        s.put(db, scope, "record", "orig", 1, {"v": "orig"})
        return {"ok": True}

    s.command(scope, "actor", "k", {"x": 1}, op)
    with pytest.raises(Conflict) as exc:
        s.command(
            scope, "actor", "k", {"x": 2}, lambda db: s.put(db, scope, "record", "alt", 1, {})
        )
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    assert [ref["id"] for ref, _ in s.list_objects(scope, "record")] == ["orig"]


def test_t003_idempotency_is_scoped_by_operation(deployment):
    """storage-model.json idempotency key = (actor_id, operation, key)."""
    s, scope = deployment.store, deployment.scope
    calls: list[str] = []
    a = s.command(
        scope, "actor", "k", {"x": 1}, lambda db: calls.append("a") or {"op": "a"}, operation="op.a"
    )
    b = s.command(
        scope, "actor", "k", {"x": 1}, lambda db: calls.append("b") or {"op": "b"}, operation="op.b"
    )
    assert a == {"op": "a"} and b == {"op": "b"}
    assert calls == ["a", "b"]


# ---------------------------------------------------------------- T-103


def test_t103_state_and_event_commit_together_or_not_at_all(deployment):
    s, scope = deployment.store, deployment.scope
    with pytest.raises(RuntimeError), s.tx() as db:
        s.cas(db, scope, "thing", "one", 0, "draft", {"v": 1})
        s.event(db, scope, "thing", "one", "thing.created", {"v": 1})
        raise RuntimeError("crash between state update and commit")
    with pytest.raises(RuntimeFault):
        s.head(scope, "thing", "one")
    assert s.events(scope) == []
    with s._lock:
        assert s.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_t103_event_without_state_is_impossible_via_api(deployment):
    """event() requires an open transaction connection; there is no autocommit path."""
    s, scope = deployment.store, deployment.scope
    with pytest.raises((RuntimeFault, sqlite3.ProgrammingError, AttributeError, TypeError)):
        s.event(None, scope, "thing", "x", "thing.created", {})


# ---------------------------------------------------------------- T-101


def test_t101_outbox_redelivery_is_deduped_by_stable_event_id(deployment):
    s, scope = deployment.store, deployment.scope
    with s.tx() as db:
        s.event(db, scope, "goal", "g", "goal.created", {"n": 1})
    delivered: list[str] = []

    def flaky(event):
        delivered.append(event["event_id"])
        return len(delivered) > 1  # first attempt "loses" the ACK

    pump = OutboxPump(s)
    first = pump.pump(flaky)
    assert first == [{"event_id": delivered[0], "acknowledged": False}]
    s.clock = lambda: 10**12  # move past the retry backoff
    second = pump.pump(flaky)
    assert second == [{"event_id": delivered[0], "acknowledged": True}]
    assert delivered[0] == delivered[1]
    assert pump.pump(flaky) == []


def test_t101_inbox_dedupes_worker_redelivery_without_second_transition(deployment, prepared):
    d = deployment
    x = d.runtime.claim(d.worker, goal_id=prepared["goal_id"])
    d.runtime.start(d.worker, x, "exact:1")
    lease = x["lease"]
    kwargs = dict(message_id="m-1", sequence=1, event_type="run.progress", payload={"p": 1})
    first = d.runtime.worker_event(
        d.worker, x["run_id"], lease["lease_id"], lease["fencing_token"], **kwargs
    )
    second = d.runtime.worker_event(
        d.worker, x["run_id"], lease["lease_id"], lease["fencing_token"], **kwargs
    )
    assert first == second
    assert d.store.head(d.scope, "run", x["run_id"])["data"]["worker_seq"] == 1
    with pytest.raises(Conflict) as exc:
        d.runtime.worker_event(
            d.worker,
            x["run_id"],
            lease["lease_id"],
            lease["fencing_token"],
            message_id="m-1",
            sequence=1,
            event_type="run.progress",
            payload={"p": "different"},
        )
    assert exc.value.code == "INBOX_CONFLICT"


# ---------------------------------------------------------------- T-099 / T-100


def test_t099_claim_committed_before_dispatch_survives_restart_with_same_correlation(
    deployment, prepared, tmp_path
):
    d = deployment
    x = d.runtime.claim(d.worker, goal_id=prepared["goal_id"])
    # "restart": the dispatch payload was committed with the lease; nothing was sent yet.
    with d.store._lock:
        rows = d.store.conn.execute(
            "SELECT dispatch_id,state FROM worker_dispatch WHERE run_id=?", (x["run_id"],)
        ).fetchall()
    assert [tuple(r) for r in rows] == [(x["dispatch_id"], "pending")]
    # A second claim for the same goal must not create a second active lease for that work.
    again = d.runtime.claim(d.worker, goal_id=prepared["goal_id"])
    assert again is None or again["lease"]["work_id"] != x["lease"]["work_id"]
    with d.store._lock:
        leases = d.store.conn.execute(
            "SELECT COUNT(*) FROM leases WHERE work_id=?", (x["lease"]["work_id"],)
        ).fetchone()[0]
    assert leases == 1
    # Re-sending the *same* dispatch is accepted with the same correlation.
    assert d.runtime.start(d.worker, x, "exact:1")["status"] == "acknowledged"
    assert d.runtime.start(d.worker, x, "exact:1")["status"] == "acknowledged"


def test_t100_spawn_ack_lost_reuses_existing_handle_not_second_spawn(deployment, prepared):
    d = deployment
    x = d.runtime.claim(d.worker, goal_id=prepared["goal_id"])
    d.runtime.start(d.worker, x, "session-A")
    with pytest.raises(Conflict) as exc:
        d.runtime.start(d.worker, x, "session-B")
    assert exc.value.code == "SESSION_CONFLICT"
    assert d.store.head(d.scope, "run", x["run_id"])["data"]["session_handle"] == "session-A"


# ---------------------------------------------------------------- T-102


def test_t102_cancel_with_unconfirmed_child_stays_pending_not_cancelled(deployment, prepared):
    d = deployment
    x = d.runtime.claim(d.worker, goal_id=prepared["goal_id"])
    d.runtime.start(d.worker, x, "exact")
    # Lease expires but the process probe cannot confirm the child stopped.
    d.store.clock = lambda: 10**12
    result = d.runtime.reap(d.scope, lambda *_: False)
    assert result and result[0]["run_id"] == x["run_id"]
    assert result[0]["process_stopped"] is False
    run = d.store.head(d.scope, "run", x["run_id"])
    assert run["state"] not in {"cancelled", "lost", "succeeded", "failed"}
    assert run["data"]["recovery_hold"] == "process_unconfirmed"


# ---------------------------------------------------------------- T-104


def test_t104_network_mount_rejected_before_open(tmp_path, monkeypatch):
    root = tmp_path / "shared"
    root.mkdir()
    fake = tmp_path / "mountinfo"
    fake.write_text(f"36 35 0:31 / {root} rw,relatime - nfs4 host:/export rw,vers=4\n")
    monkeypatch.setattr(store_module, "MOUNTINFO", fake)
    with pytest.raises(Hold) as exc:
        Store(root)
    assert exc.value.code == "UNSUPPORTED_STORAGE"
    assert not (root / "runtime.sqlite3").exists()


def test_t104_unknown_platform_requires_explicit_local_qualification(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "MOUNTINFO", tmp_path / "absent")
    # No mountinfo (macOS): the store opens, but reports that local-disk qualification
    # is delegated to the deployment doctor rather than silently claiming it.
    with Store(tmp_path / "s") as s:
        assert s.storage_qualification["filesystem"] == "unverified_no_mountinfo"


# ---------------------------------------------------------------- T-105


def test_t105_restore_raises_epoch_and_blocks_admission_until_reconciled(
    deployment, prepared, tmp_path
):
    from dataclasses import replace

    d = deployment
    d.execute(prepared)
    actor = replace(d.actor, permissions=d.actor.permissions | {"runtime.backup"})
    backup = tmp_path / "backup"
    RecoveryService(d.store).backup(actor, backup)
    out = RecoveryService.restore(backup, tmp_path / "restore", operator_confirmed=True)
    assert out["status"] == "restored_admission_disabled"
    with Store(tmp_path / "restore") as s:
        assert s.epoch > d.store.epoch
        kill = s.head(d.scope, "runtime-control", "kill")
        assert kill["data"]["enabled"] and "reconcile" in kill["data"]["reason"]
        # Old leases belong to the old epoch and are stale by construction.
        with s._lock:
            stale = s.conn.execute(
                "SELECT COUNT(*) FROM leases WHERE epoch<>?", (s.epoch,)
            ).fetchone()[0]
            total = s.conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
        assert stale == total


# ---------------------------------------------------------------- T-106


def test_t106_disk_full_during_artifact_leaves_no_registry_entry_and_no_temp(
    deployment, monkeypatch
):
    d = deployment
    artifacts = ArtifactStore(d.store)
    real_fdopen = os.fdopen

    class Full:
        def __init__(self, fd):
            self.fd = fd

        def write(self, data):
            raise OSError(errno.ENOSPC, "No space left on device")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            os.close(self.fd)

    monkeypatch.setattr(os, "fdopen", lambda fd, *a, **k: Full(fd))
    with pytest.raises(OSError):
        artifacts.admit(d.scope, b'{"big": true}', "application/json")
    monkeypatch.setattr(os, "fdopen", real_fdopen)
    with d.store._lock:
        assert d.store.conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
    directory = artifacts._directory(d.scope)
    assert not [p for p in Path(directory).glob(".upload-*")]
    assert artifacts.inventory(d.scope)["orphans"] == []


def test_t106_registry_commit_failure_leaves_only_orphan_never_false_reference(
    deployment, monkeypatch
):
    d = deployment
    artifacts = ArtifactStore(d.store)
    original_event = d.store.event

    def failing_event(*args, **kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(d.store, "event", failing_event)
    with pytest.raises(sqlite3.OperationalError):
        artifacts.admit(d.scope, b'{"x":1}', "application/json")
    monkeypatch.setattr(d.store, "event", original_event)
    with d.store._lock:
        assert d.store.conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
    assert len(artifacts.inventory(d.scope)["orphans"]) == 1
