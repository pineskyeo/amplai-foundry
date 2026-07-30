from __future__ import annotations

import multiprocessing
import sqlite3
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from amplai_foundry.governance import (
    ChannelProvider,
    ChannelRef,
    DecisionAction,
    IngressConfig,
    IngressError,
    IngressLeaseConflictError,
    IngressService,
    IngressState,
    ProviderEnvelope,
    VerifiedProviderCommand,
)
from amplai_foundry.governance.store import GovernanceStore, GovernanceStoreError

NOW = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
RAW_TOKEN = "raw-secret-action-token"
RAW_BODY = b'{"event_id":"EVT-1","token":"raw-secret-action-token"}'
CHANNEL = ChannelRef(
    provider=ChannelProvider.SLACK,
    workspace_id="T123",
    channel_id="C456",
    message_id="1710000000.000200",
)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class FakeAuthenticator:
    def __init__(self) -> None:
        self.calls = 0

    def verify(self, envelope: ProviderEnvelope) -> VerifiedProviderCommand:
        self.calls += 1
        if envelope.headers.get("x-valid") != "yes":
            raise IngressError("PROVIDER_AUTHENTICITY_FAILED")
        return VerifiedProviderCommand(
            external_event_id="EVT-1",
            external_actor_key="U456",
            channel_ref=CHANNEL,
            credential_id="TOK-0123456789ABCDEF",
            raw_credential=RAW_TOKEN,
            action=DecisionAction.APPROVE,
        )


class BlockingCommitStore(GovernanceStore):
    def __init__(self, path: Path, marker: Path) -> None:
        super().__init__(path)
        self.marker = marker

    @contextmanager
    def connect(self, *, busy_timeout_ms: int | None = None) -> Iterator[sqlite3.Connection]:
        with super().connect(busy_timeout_ms=busy_timeout_ms) as connection:

            def trace(statement: str) -> None:
                if statement == "COMMIT":
                    self.marker.write_text("ready", encoding="utf-8")
                    time.sleep(60)

            connection.set_trace_callback(trace)
            yield connection


class UnavailableStore(GovernanceStore):
    @contextmanager
    def connect(self, *, busy_timeout_ms: int | None = None) -> Iterator[sqlite3.Connection]:
        del busy_timeout_ms
        raise GovernanceStoreError("injected unavailable")
        yield  # pragma: no cover


def _blocking_accept(database: str, marker: str) -> None:
    service = IngressService(
        BlockingCommitStore(Path(database), Path(marker)),
        FakeAuthenticator(),
        clock=lambda: NOW,
    )
    service.accept(_envelope())


def _blocking_worker(database: str, marker: str) -> None:
    service = IngressService(
        GovernanceStore(Path(database)),
        FakeAuthenticator(),
        config=IngressConfig(lease_duration=timedelta(seconds=1)),
        clock=lambda: NOW,
    )
    claimed = service.claim_next("worker-killed")
    if claimed is None:
        raise RuntimeError("expected pending command")
    Path(marker).write_text("ready", encoding="utf-8")
    time.sleep(60)


def _envelope(*, body: bytes = RAW_BODY, valid: bool = True) -> ProviderEnvelope:
    return ProviderEnvelope(
        provider=ChannelProvider.SLACK,
        provider_installation_ref="T123:APP1",
        raw_body=body,
        headers={"x-valid": "yes" if valid else "no"},
    )


def _service(
    tmp_path: Path,
    *,
    config: IngressConfig | None = None,
) -> tuple[GovernanceStore, FakeAuthenticator, MutableClock, IngressService]:
    store = GovernanceStore(tmp_path / "governance.db")
    store.initialize()
    authenticator = FakeAuthenticator()
    clock = MutableClock(NOW)
    return (
        store,
        authenticator,
        clock,
        IngressService(
            store,
            authenticator,
            config=config,
            clock=clock,
        ),
    )


def test_accept_authenticates_then_commits_hash_only_command_and_replays(tmp_path: Path) -> None:
    store, authenticator, _clock, service = _service(tmp_path)

    first = service.accept(_envelope())
    replay = service.accept(_envelope())

    assert first.accepted and first.state is IngressState.PENDING
    assert replay.accepted and replay.duplicate
    assert replay.command_id == first.command_id
    assert authenticator.calls == 2
    command = service.get(first.command_id or "")
    assert command is not None
    assert command.provider is ChannelProvider.SLACK
    assert command.external_event_id == "EVT-1"
    assert command.credential_id == "TOK-0123456789ABCDEF"
    assert command.credential_hash.startswith("sha256:")
    assert command.raw_body_digest.startswith("sha256:")
    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM governance_ingress_commands"
        ).fetchone() == (1,)
    durable_bytes = b"".join(
        path.read_bytes()
        for path in store.path.parent.glob(f"{store.path.name}*")
        if path.is_file()
    )
    assert RAW_TOKEN.encode() not in durable_bytes
    assert RAW_BODY not in durable_bytes


def test_size_and_authenticity_fail_before_durable_write(tmp_path: Path) -> None:
    store, authenticator, _clock, service = _service(
        tmp_path,
        config=IngressConfig(max_raw_body_bytes=8),
    )
    with pytest.raises(IngressError, match="INGRESS_PAYLOAD_TOO_LARGE"):
        service.accept(_envelope(body=b"x" * 9))
    assert authenticator.calls == 0

    with pytest.raises(IngressError, match="PROVIDER_AUTHENTICITY_FAILED"):
        service.accept(_envelope(body=b"small", valid=False))
    assert authenticator.calls == 1
    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM governance_ingress_commands"
        ).fetchone() == (0,)


def test_same_external_scope_with_changed_body_is_replay_conflict(tmp_path: Path) -> None:
    store, _authenticator, _clock, service = _service(tmp_path)
    first = service.accept(_envelope())

    with pytest.raises(IngressError, match="INGRESS_REPLAY_CONFLICT"):
        service.accept(_envelope(body=b'{"event_id":"EVT-1","changed":true}'))

    assert service.get(first.command_id or "") is not None
    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM governance_ingress_commands"
        ).fetchone() == (1,)


def test_busy_ingress_returns_non_success_within_three_seconds(tmp_path: Path) -> None:
    store, authenticator, clock, _service_value = _service(tmp_path)
    service = IngressService(
        store,
        authenticator,
        config=IngressConfig(busy_timeout_ms=50),
        clock=clock,
    )
    blocker = sqlite3.connect(store.path, isolation_level=None)
    blocker.execute("PRAGMA journal_mode = WAL")
    blocker.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        ack = service.accept(_envelope())
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert not ack.accepted
    assert ack.error_code == "INGRESS_UNAVAILABLE"
    assert time.monotonic() - started < 3
    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM governance_ingress_commands"
        ).fetchone() == (0,)


def test_hard_kill_before_accept_commit_leaves_no_command(tmp_path: Path) -> None:
    store, _authenticator, _clock, _service_value = _service(tmp_path)
    marker = tmp_path / "commit-ready"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_blocking_accept,
        args=(str(store.path), str(marker)),
    )
    process.start()
    deadline = time.monotonic() + 5
    while not marker.exists() and process.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists(), "ingress child가 COMMIT checkpoint에 도달하지 못했습니다."
    process.kill()
    process.join(timeout=5)
    assert not process.is_alive()

    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM governance_ingress_commands"
        ).fetchone() == (0,)


def test_unavailable_ingress_never_returns_accepted(tmp_path: Path) -> None:
    path = tmp_path / "governance.db"
    GovernanceStore(path).initialize()
    service = IngressService(
        UnavailableStore(path),
        FakeAuthenticator(),
        clock=lambda: NOW,
    )

    started = time.monotonic()
    ack = service.accept(_envelope())

    assert not ack.accepted and ack.error_code == "INGRESS_UNAVAILABLE"
    assert time.monotonic() - started < 3


def test_lease_expiry_reclaim_fences_stale_worker_and_completes_once(tmp_path: Path) -> None:
    _store, _authenticator, clock, service = _service(
        tmp_path,
        config=IngressConfig(lease_duration=timedelta(seconds=10)),
    )
    ack = service.accept(_envelope())
    first = service.claim_next("worker-1")
    assert first is not None
    assert first.state is IngressState.LEASED
    assert first.attempts == 1 and first.claim_generation == 1
    assert service.claim_next("worker-2") is None

    clock.value = NOW + timedelta(seconds=11)
    reclaimed = service.claim_next("worker-2")
    assert reclaimed is not None
    assert reclaimed.command_id == ack.command_id
    assert reclaimed.attempts == 2 and reclaimed.claim_generation == 2
    with pytest.raises(IngressLeaseConflictError):
        service.complete(
            reclaimed.command_id,
            worker_id="worker-1",
            generation=first.claim_generation,
        )
    completed = service.complete(
        reclaimed.command_id,
        worker_id="worker-2",
        generation=reclaimed.claim_generation,
    )
    assert completed.state is IngressState.COMPLETED
    assert completed.completed_at == clock.value


def test_retry_backoff_is_bounded_and_exhaustion_dead_letters(tmp_path: Path) -> None:
    _store, _authenticator, clock, service = _service(
        tmp_path,
        config=IngressConfig(
            max_attempts=2,
            retry_base=timedelta(seconds=5),
            retry_cap=timedelta(seconds=30),
        ),
    )
    ack = service.accept(_envelope())
    first = service.claim_next("worker")
    assert first is not None
    retry_one = service.retry(
        first.command_id,
        worker_id="worker",
        generation=first.claim_generation,
        error_code="TRANSIENT",
    )
    assert retry_one.retry_at == NOW + timedelta(seconds=5)
    assert service.claim_next("worker") is None

    clock.value = NOW + timedelta(seconds=5)
    second = service.claim_next("worker")
    assert second is not None and second.attempts == 2
    retry_two = service.retry(
        second.command_id,
        worker_id="worker",
        generation=second.claim_generation,
        error_code="TRANSIENT",
    )
    assert retry_two.retry_at == clock.value + timedelta(seconds=10)

    clock.value = retry_two.retry_at
    assert service.claim_next("worker") is None
    exhausted = service.get(ack.command_id or "")
    assert exhausted is not None
    assert exhausted.state is IngressState.DEAD_LETTER


def test_concurrent_claim_has_one_winner(tmp_path: Path) -> None:
    _store, _authenticator, _clock, service = _service(tmp_path)
    service.accept(_envelope())

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(service.claim_next, ("worker-1", "worker-2")))

    assert sum(result is not None for result in results) == 1
    claimed = next(result for result in results if result is not None)
    assert claimed.attempts == 1 and claimed.claim_generation == 1


def test_recovery_hold_is_terminal_for_automatic_claim(tmp_path: Path) -> None:
    _store, _authenticator, _clock, service = _service(tmp_path)
    ack = service.accept(_envelope())
    claim = service.claim_next("worker")
    assert claim is not None

    held = service.recovery_hold(
        claim.command_id,
        worker_id="worker",
        generation=claim.claim_generation,
        error_code="RESULT_UNKNOWN",
    )

    assert held.state is IngressState.RECOVERY_HOLD
    assert held.last_error_code == "RESULT_UNKNOWN"
    assert service.claim_next("other-worker") is None
    assert service.get(ack.command_id or "") == held


def test_hard_killed_worker_is_reclaimed_after_lease_expiry(tmp_path: Path) -> None:
    store, authenticator, _clock, service = _service(
        tmp_path,
        config=IngressConfig(lease_duration=timedelta(seconds=1)),
    )
    ack = service.accept(_envelope())
    marker = tmp_path / "worker-claimed"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_blocking_worker,
        args=(str(store.path), str(marker)),
    )
    process.start()
    deadline = time.monotonic() + 5
    while not marker.exists() and process.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists(), "worker child가 claim checkpoint에 도달하지 못했습니다."
    process.kill()
    process.join(timeout=5)
    assert not process.is_alive()

    recovery_clock = MutableClock(NOW + timedelta(seconds=2))
    recovery = IngressService(
        store,
        authenticator,
        config=IngressConfig(lease_duration=timedelta(seconds=1)),
        clock=recovery_clock,
    )
    reclaimed = recovery.claim_next("worker-recovery")
    assert reclaimed is not None
    assert reclaimed.command_id == ack.command_id
    assert reclaimed.attempts == 2 and reclaimed.claim_generation == 2
