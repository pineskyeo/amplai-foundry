from __future__ import annotations

import hashlib
import json
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
from amplai_foundry.governance.store import (
    GovernanceStore,
    GovernanceStoreError,
    GovernanceTransactionError,
)

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
    def __init__(
        self,
        *,
        credential_id: str = "TOK-0123456789ABCDEF",
        raw_credential: str = RAW_TOKEN,
    ) -> None:
        self.calls = 0
        self.credential_id = credential_id
        self.raw_credential = raw_credential

    def verify(self, envelope: ProviderEnvelope) -> VerifiedProviderCommand:
        self.calls += 1
        if envelope.headers.get("x-valid") != "yes":
            raise IngressError("PROVIDER_AUTHENTICITY_FAILED")
        return VerifiedProviderCommand(
            external_event_id="EVT-1",
            external_actor_key="U456",
            channel_ref=CHANNEL,
            credential_id=self.credential_id,
            raw_credential=self.raw_credential,
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


class CommitAfterSuccessConnection:
    def __init__(self, connection: sqlite3.Connection, store: CommitAfterSuccessStore) -> None:
        self._connection = connection
        self._store = store

    @property
    def in_transaction(self) -> bool:
        return self._connection.in_transaction

    def execute(self, statement: str, parameters: object = ()) -> sqlite3.Cursor:
        if parameters == ():
            cursor = self._connection.execute(statement)
        else:
            cursor = self._connection.execute(statement, parameters)  # type: ignore[arg-type]
        if statement == "COMMIT" and self._store.fail_next_commit:
            self._store.fail_next_commit = False
            raise sqlite3.OperationalError("injected after durable commit")
        return cursor


class CommitAfterSuccessStore(GovernanceStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.fail_next_commit = True

    @contextmanager
    def connect(self, *, busy_timeout_ms: int | None = None) -> Iterator[sqlite3.Connection]:
        with super().connect(busy_timeout_ms=busy_timeout_ms) as connection:
            yield CommitAfterSuccessConnection(connection, self)  # type: ignore[misc]


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


def _seed_action_token(store: GovernanceStore) -> None:
    digest = f"sha256:{'1' * 64}"
    channel_json = json.dumps(
        CHANNEL.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    with store.connect() as connection:
        connection.execute(
            """
            INSERT INTO governance_active_proposals(
                project_namespace, project_id, proposal_id, active_definition_digest,
                content_revision, state_revision, decision_epoch, status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, 2, 1, 'reviewed', ?, ?)
            """,
            (
                "org/default/project/amplai",
                "amplai",
                "PROP-20260730-ABCDEF12",
                digest,
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO governance_action_tokens(
                token_id, token_hash, project_namespace, project_id, proposal_id,
                active_definition_digest, content_revision, state_revision,
                decision_epoch, allowed_action, allowed_actor_id, allowed_actor_type,
                bound_channel_json, issued_at, expires_at, state, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, 2, 1, 'approve', ?, 'human', ?, ?, ?, 'issued', NULL)
            """,
            (
                "TOK-0123456789ABCDEF",
                f"sha256:{hashlib.sha256(RAW_TOKEN.encode()).hexdigest()}",
                "org/default/project/amplai",
                "amplai",
                "PROP-20260730-ABCDEF12",
                digest,
                "ACT-HUMAN-1",
                channel_json,
                NOW.isoformat(),
                (NOW + timedelta(minutes=15)).isoformat(),
            ),
        )


def _service(
    tmp_path: Path,
    *,
    config: IngressConfig | None = None,
) -> tuple[GovernanceStore, FakeAuthenticator, MutableClock, IngressService]:
    store = GovernanceStore(tmp_path / "governance.db")
    store.initialize()
    _seed_action_token(store)
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


def test_new_ingress_requires_matching_durable_action_token(tmp_path: Path) -> None:
    store, _authenticator, clock, _service_value = _service(tmp_path)
    unknown = IngressService(
        store,
        FakeAuthenticator(
            credential_id="TOK-FEDCBA9876543210",
            raw_credential="unknown-action-token",
        ),
        clock=clock,
    )

    with pytest.raises(IngressError, match="ACTION_TOKEN_INVALID"):
        unknown.accept(_envelope())

    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM governance_ingress_commands"
        ).fetchone() == (0,)


def test_authenticator_cannot_persist_raw_credential_as_credential_id(tmp_path: Path) -> None:
    store, _authenticator, clock, _service_value = _service(tmp_path)
    secret_shaped_as_id = "TOK-AAAAAAAAAAAAAAAA"
    unsafe = IngressService(
        store,
        FakeAuthenticator(
            credential_id=secret_shaped_as_id,
            raw_credential=secret_shaped_as_id,
        ),
        clock=clock,
    )

    with pytest.raises(IngressError, match="PROVIDER_PAYLOAD_INVALID"):
        unsafe.accept(_envelope())

    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM governance_ingress_commands"
        ).fetchone() == (0,)
    durable_bytes = b"".join(
        path.read_bytes()
        for path in store.path.parent.glob(f"{store.path.name}*")
        if path.is_file()
    )
    assert secret_shaped_as_id.encode() not in durable_bytes


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


def test_commit_after_success_ambiguity_reconciles_on_provider_replay(tmp_path: Path) -> None:
    store, authenticator, clock, service = _service(tmp_path)
    ambiguous = IngressService(
        CommitAfterSuccessStore(store.path),
        authenticator,
        clock=clock,
    )

    first = ambiguous.accept(_envelope())
    replay = service.accept(_envelope())

    assert not first.accepted and first.error_code == "GOVERNANCE_COMMIT_AMBIGUOUS"
    assert replay.accepted and replay.duplicate
    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM governance_ingress_commands"
        ).fetchone() == (1,)


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


def test_expired_lease_owner_cannot_finalize_before_reclaim(tmp_path: Path) -> None:
    _store, _authenticator, clock, service = _service(
        tmp_path,
        config=IngressConfig(lease_duration=timedelta(seconds=1)),
    )
    service.accept(_envelope())
    claim = service.claim_next("worker")
    assert claim is not None
    clock.value = NOW + timedelta(seconds=2)

    with pytest.raises(IngressLeaseConflictError):
        service.complete(
            claim.command_id,
            worker_id="worker",
            generation=claim.claim_generation,
        )

    current = service.get(claim.command_id)
    assert current is not None and current.state is IngressState.LEASED


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


# ---------------------------------------------------------------------------
# MGC-012-P5-T010 — 한 저장소 한 정책 (round 11 R-2)
# ---------------------------------------------------------------------------


def _sqlite_error(message: str, errorcode: int | None) -> sqlite3.Error:
    error = (
        sqlite3.DatabaseError(message)
        if errorcode is not None
        else sqlite3.OperationalError(message)
    )
    if errorcode is not None:
        error.sqlite_errorcode = errorcode
    return error


# T010 AC-04 — ingress 경로와 배달 경로가 같은 판별기를 쓴다.
#
# `R-2` 의 본질은 한 저장소에서 같은 예외가 두 정책을 받은 것이다. ingress 는
# `sqlite3.OperationalError` 를 재시도로 분류하는데 배달 경로는 즉시 되돌릴 수 없는 hold 로
# 보냈다. 이름이 같은 두 함수를 비교하면 다시 갈라져도 안 잡히므로 **같은 함수 객체인지**
# 본다.
def test_both_paths_consult_one_store_failure_classifier() -> None:
    from amplai_foundry.governance import ingress_worker, slack_projection, store

    assert ingress_worker.is_store_corruption is store.is_store_corruption
    assert slack_projection.is_transient_store_failure is store.is_transient_store_failure
    assert not hasattr(ingress_worker, "_is_corruption")


# T010 AC-04 — 판별기의 verdict 를 값별로 고정한다. 손상만 terminal 이다.
@pytest.mark.parametrize(
    ("error", "transient"),
    [
        (_sqlite_error("database is locked", None), True),
        (_sqlite_error("disk I/O error", None), True),
        (_sqlite_error("database or disk is full", None), True),
        (_sqlite_error("attempt to write a readonly database", None), True),
        # RL-2 — `GovernanceStoreError` 가 빠지면 `R-2` 의 guard 자체가 사라진다. ingress
        # test 가 실제로 던지는 것이 이 형이다 ("ingress decision connection is busy").
        (GovernanceStoreError("ingress decision connection is busy"), True),
        (GovernanceTransactionError("transaction could not complete"), True),
        (_sqlite_error("database disk image is malformed", sqlite3.SQLITE_CORRUPT), False),
        (_sqlite_error("file is not a database", sqlite3.SQLITE_NOTADB), False),
        (RuntimeError("정체 불명"), False),
    ],
)
def test_the_shared_classifier_treats_only_corruption_as_terminal(
    error: BaseException, transient: bool
) -> None:
    from amplai_foundry.governance.store import is_transient_store_failure

    assert is_transient_store_failure(error) is transient


# T010 AC-04 — 확장 error code 를 써도 하위 byte 로 손상을 알아본다. 이 계산을 지우면
# 확장 code 를 든 손상이 재시도로 흘러 destination 이 안전망을 잃는다.
def test_an_extended_corruption_code_is_still_corruption() -> None:
    from amplai_foundry.governance.store import is_store_corruption

    extended = sqlite3.DatabaseError("corrupt index")
    extended.sqlite_errorcode = sqlite3.SQLITE_CORRUPT | (1 << 8)

    assert is_store_corruption(extended) is True
