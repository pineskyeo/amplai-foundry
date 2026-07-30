from __future__ import annotations

import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance import (
    ActiveProposalRepository,
    ActorBindingService,
    ActorRef,
    ActorType,
    AuthorityPermission,
    AuthorityService,
    BindingApproval,
    BindingTarget,
    ChannelProvider,
    ChannelRef,
    DecisionAction,
    DecisionError,
    DecisionService,
    DirectAuthorityRequest,
    ImmutableDefinitionObjectStore,
    OutboxDispatcher,
    OutboxState,
    ProposalDefinitionManifest,
    ProposalRef,
    ProposalSubmissionService,
    canonicalize_definition,
)
from amplai_foundry.governance.apply_jobs import (
    ApplyGovernanceError,
    ApplyGrantService,
    ApplyJobService,
    ApplyRequestService,
)
from amplai_foundry.governance.events import GovernanceEventError
from amplai_foundry.governance.migrations import INITIAL_MIGRATIONS
from amplai_foundry.governance.publish import ProjectPublishGateView, PublishGateState
from amplai_foundry.governance.store import GovernanceStore, governance_transaction

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
PROJECT = ProjectRef(project_id="amplai", namespace="org/default/project/amplai")
AUTHORITY_PROJECT = ProjectRef(project_id="governance", namespace="org/default/project/governance")
PROPOSAL = ProposalRef(project_ref=PROJECT, proposal_id="PROP-20260730-ABCDEF12")
ACTOR = ActorRef(actor_id="ACT-APPLIER-1", actor_type=ActorType.HUMAN)
MANAGER = ActorRef(actor_id="ACT-MANAGER-1", actor_type=ActorType.HUMAN)
CHANNEL = ChannelRef(
    provider=ChannelProvider.SLACK,
    workspace_id="T123",
    channel_id="C456",
    message_id="1710000000.000200",
)


def _approval(index: int) -> BindingApproval:
    return BindingApproval(
        approval_id=f"APR-{index:016X}",
        approved_by=MANAGER,
        reason="approve apply grant fixture",
    )


def _request() -> DirectAuthorityRequest:
    return DirectAuthorityRequest(
        provider=ChannelProvider.SLACK,
        provider_installation_ref="T123:APP1",
        external_actor_id="U123",
        project_ref=PROJECT,
        request_id="request-apply-1",
        channel=CHANNEL,
    )


def _fixture(
    tmp_path: Path,
    *,
    grant_apply_permission: bool = True,
    include_action_token: bool = False,
):
    store = GovernanceStore(tmp_path / "governance.db")
    store.initialize()
    objects = ImmutableDefinitionObjectStore(PROJECT, tmp_path)
    canonical = canonicalize_definition(
        ProposalDefinitionManifest(
            proposal_ref=PROPOSAL,
            operations=({"marker": "apply", "type": "CREATE"},),
            base_revision="a13d92f",
            validation_policy_ref="policy/proposal-v3",
        )
    )
    object_ref = objects.put_definition_object(
        PROPOSAL, canonical.canonical_bytes, canonical.digest
    )
    active = ActiveProposalRepository(store, objects)
    draft = active.activate_definition_revision(
        PROPOSAL,
        expected_active_digest=None,
        expected_state_revision=0,
        next_object_ref=object_ref,
    )
    bindings = ActorBindingService(store, AUTHORITY_PROJECT, clock=lambda: NOW)
    bindings.bootstrap_manager(MANAGER)
    bindings.register_actor(ACTOR, approval=_approval(1))
    permissions = [
        AuthorityPermission.PROPOSAL_READ,
        AuthorityPermission.PROPOSAL_SUBMIT_REVIEW,
        AuthorityPermission.PROPOSAL_DECIDE,
    ]
    if grant_apply_permission:
        permissions.append(AuthorityPermission.PROPOSAL_REQUEST_APPLY)
    for index, permission in enumerate(permissions, start=2):
        bindings.grant_permission(ACTOR, PROJECT, permission, approval=_approval(index))
    bindings.create_binding(
        BindingTarget(
            provider=ChannelProvider.SLACK,
            provider_installation_ref="T123:APP1",
            external_actor_id="U123",
            actor_ref=ACTOR,
        ),
        approval=_approval(8),
    )
    authority = AuthorityService(store, clock=lambda: NOW)
    ProposalSubmissionService(store, active, authority).submit_for_review(
        PROPOSAL,
        authority_request=_request(),
        expected_state_revision=draft.state_revision,
    )
    decisions = DecisionService(store, authority, clock=lambda: NOW)
    approve = next(
        token
        for token in decisions.issue_tokens(PROPOSAL, authority_request=_request())
        if token.record.allowed_action is DecisionAction.APPROVE
    )
    decision_key = "approved-decision-1"
    decisions.decide(
        PROPOSAL,
        action=DecisionAction.APPROVE,
        authority_request=_request(),
        raw_token=approve.raw_token,
        idempotency_key=decision_key,
        request_fingerprint=hashlib.sha256(decision_key.encode()).hexdigest(),
    )
    result = (
        store,
        active,
        ApplyGrantService(store, authority, objects, clock=lambda: NOW),
        decision_key,
    )
    return (*result, approve.raw_token) if include_action_token else result


def _queued_job_fixture(tmp_path: Path):
    store, _active, grants, decision_key = _fixture(tmp_path)
    issued = grants._issue_from_approved_decision(decision_key, authority_request=_request())
    request = ApplyRequestService(store, grants.authority_service, clock=lambda: NOW)
    result = request.request_apply(
        PROPOSAL,
        authority_request=_request(),
        raw_grant=issued.raw_grant,
        idempotency_key="queued-job-apply-1",
        request_fingerprint=hashlib.sha256(b"queued-job-apply-1").hexdigest(),
    )
    return store, result.job_id


def _publish_pending_job_fixture(tmp_path: Path):
    store, job_id = _queued_job_fixture(tmp_path)
    jobs = ApplyJobService(store, clock=lambda: NOW)
    leased = jobs.claim_next("worker-publish-fixture")
    assert leased is not None
    jobs.start(
        job_id,
        worker_id="worker-publish-fixture",
        fencing_token=leased.fencing_token,
    )
    published = jobs.stage_for_publish(
        job_id,
        worker_id="worker-publish-fixture",
        fencing_token=leased.fencing_token,
        artifact_bytes=b"immutable staged tree",
        publish_request_bytes=b'{"canonical_ref":"refs/heads/main"}',
    )
    return store, published


def _insert_prepared_publish_foundation(store: GovernanceStore, job_id: str) -> str:
    intent_id = "PBI-0000000000000001"
    with store.connect() as connection:
        row = connection.execute(
            """
            SELECT snapshot_id, project_namespace, project_id, proposal_id,
                   fencing_token, approved_snapshot_digest, expected_base_revision,
                   staged_artifact_digest, publish_request_digest
            FROM governance_apply_jobs WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
        assert row is not None
        connection.execute(
            """
            INSERT INTO governance_publish_intents(
                intent_id, job_id, snapshot_id, project_namespace, project_id,
                proposal_id, fencing_token, approved_snapshot_digest,
                expected_base_revision, staged_artifact_digest, publish_request_digest,
                canonical_ref, expected_old_ref, candidate_commit, candidate_tree_digest,
                status, prepared_at, resolved_at, last_error_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, NULL, NULL)
            """,
            (
                intent_id,
                job_id,
                *row,
                "refs/heads/main",
                "a" * 40,
                "b" * 40,
                f"sha256:{'c' * 64}",
                NOW.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO governance_project_publish_gates(
                project_namespace, project_id, canonical_ref, active_intent_id,
                gate_revision, state, updated_at
            ) VALUES (?, ?, 'refs/heads/main', ?, 1, 'locked', ?)
            """,
            (PROJECT.namespace, PROJECT.project_id, intent_id, NOW.isoformat()),
        )
    return intent_id


def test_approved_decision_issues_snapshot_scoped_hash_only_grant(tmp_path: Path) -> None:
    store, _active, grants, decision_key = _fixture(tmp_path)
    delayed = ApplyGrantService(
        store,
        grants.authority_service,
        grants.definitions,
        clock=lambda: NOW + timedelta(days=7),
    )

    issued = delayed._issue_from_approved_decision(
        decision_key,
        authority_request=_request(),
    )

    assert issued.record.proposal_ref == PROPOSAL
    assert issued.record.allowed_actor_ref == ACTOR
    assert issued.record.bound_channel_ref == CHANNEL
    assert issued.record.allowed_action == "request_apply"
    assert "raw_grant=<redacted>" in repr(issued)
    snapshot = delayed.get_snapshot(issued.record.snapshot_id)
    assert snapshot.expected_base_revision == "a13d92f"
    assert snapshot.approved_at == NOW
    assert issued.record.issued_at == NOW + timedelta(days=7)
    assert snapshot.snapshot_digest == issued.record.approved_snapshot_digest
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_approved_snapshots"
        ).fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM governance_apply_grants").fetchone() == (1,)
        stored = "\n".join(
            str(value)
            for row in connection.execute(
                "SELECT grant_id, grant_hash, bound_channel_json FROM governance_apply_grants"
            ).fetchall()
            for value in row
        )
    assert issued.raw_grant not in stored
    for database_file in tmp_path.glob("governance.db*"):
        assert issued.raw_grant.encode() not in database_file.read_bytes()


def test_apply_grant_issue_requires_request_apply_authority(tmp_path: Path) -> None:
    _store, _active, grants, decision_key = _fixture(
        tmp_path,
        grant_apply_permission=False,
    )

    with pytest.raises(ApplyGovernanceError, match="AUTHORITY_DENIED"):
        grants._issue_from_approved_decision(decision_key, authority_request=_request())


def test_apply_grant_issue_rejects_stale_approved_snapshot(tmp_path: Path) -> None:
    store, _active, grants, decision_key = _fixture(tmp_path)
    with store.connect() as connection:
        connection.execute(
            """
            UPDATE governance_active_proposals
            SET status = 'apply_requested', state_revision = state_revision + 1
            """
        )

    with pytest.raises(ApplyGovernanceError, match="APPROVED_SNAPSHOT_STALE"):
        grants._issue_from_approved_decision(decision_key, authority_request=_request())


def test_apply_snapshot_and_grant_issuance_fields_are_immutable(tmp_path: Path) -> None:
    store, _active, grants, decision_key = _fixture(tmp_path)
    issued = grants._issue_from_approved_decision(decision_key, authority_request=_request())

    with store.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="snapshot is immutable"):
            connection.execute(
                "UPDATE governance_approved_snapshots SET expected_base_revision = 'bbbbbbb'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="grant issuance is immutable"):
            connection.execute(
                "UPDATE governance_apply_grants SET grant_hash = ? WHERE grant_id = ?",
                (f"sha256:{'f' * 64}", issued.record.grant_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="grant is durable"):
            connection.execute(
                "DELETE FROM governance_apply_grants WHERE grant_id = ?",
                (issued.record.grant_id,),
            )


def test_duplicate_concurrent_card_issue_creates_one_issued_grant(tmp_path: Path) -> None:
    store, _active, grants, decision_key = _fixture(tmp_path)
    barrier = threading.Barrier(2)

    def issue():
        barrier.wait()
        try:
            return grants._issue_from_approved_decision(
                decision_key,
                authority_request=_request(),
            )
        except ApplyGovernanceError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _index: issue(), range(2)))
    assert sum(not isinstance(result, str) for result in results) == 1
    assert "APPLY_GRANT_ALREADY_ISSUED" in results
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_apply_grants WHERE state = 'issued'"
        ).fetchone() == (1,)


def test_snapshot_identity_ttl_and_job_identity_are_relationally_enforced(
    tmp_path: Path,
) -> None:
    store, _active, grants, decision_key = _fixture(tmp_path)
    issued = grants._issue_from_approved_decision(decision_key, authority_request=_request())
    with store.connect() as connection:
        snapshot = connection.execute(
            """
            SELECT snapshot_id, project_namespace, project_id, proposal_id,
                   snapshot_digest, content_revision, state_revision, decision_epoch,
                   expected_base_revision
            FROM governance_approved_snapshots
            """
        ).fetchone()
        assert snapshot is not None
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO governance_apply_grants(
                    grant_id, grant_hash, snapshot_id, project_namespace, project_id,
                    proposal_id, approved_snapshot_digest, content_revision,
                    state_revision, decision_epoch, allowed_action, allowed_actor_id,
                    allowed_actor_type, bound_channel_json, issued_at, expires_at,
                    state, resolved_at
                ) VALUES (
                    'AGR-EVIL', ?, ?, 'evil/ns', 'evil', ?, ?, 999, 999, 999,
                    'request_apply', 'ACT-APPLIER-1', 'human', ?, ?, ?, 'issued', NULL
                )
                """,
                (
                    f"sha256:{'e' * 64}",
                    snapshot[0],
                    PROPOSAL.proposal_id,
                    f"sha256:{'f' * 64}",
                    CHANNEL.model_dump_json(exclude_none=True),
                    NOW.isoformat(),
                    (NOW - timedelta(seconds=1)).isoformat(),
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO governance_apply_jobs(
                    job_id, snapshot_id, project_namespace, project_id, proposal_id,
                    approved_snapshot_digest, expected_base_revision, status,
                    attempts, fencing_token, lease_owner, lease_expires_at, retry_at,
                    staged_artifact_digest, publish_request_digest, last_error_code,
                    created_at, updated_at
                ) VALUES (
                    'JOB-EVIL', ?, 'evil/ns', 'evil', ?, ?, 'not-a-revision',
                    'queued', 0, 0, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?
                )
                """,
                (
                    snapshot[0],
                    PROPOSAL.proposal_id,
                    f"sha256:{'f' * 64}",
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
            )
    assert grants.get_grant(issued.record.grant_id) == issued.record


def test_raw_apply_grant_cannot_persist_as_decision_idempotency_key(tmp_path: Path) -> None:
    store, _active, grants, decision_key = _fixture(tmp_path)
    issued = grants._issue_from_approved_decision(decision_key, authority_request=_request())
    decisions = DecisionService(store, grants.authority_service, clock=lambda: NOW)

    with pytest.raises(DecisionError, match="IDEMPOTENCY_CONFLICT"):
        decisions.decide(
            PROPOSAL,
            action=DecisionAction.REJECT,
            authority_request=_request(),
            raw_token="not-a-real-token",
            idempotency_key=f"prefix-{issued.raw_grant}-suffix",
            request_fingerprint=hashlib.sha256(b"cross-secret").hexdigest(),
        )
    with store.connect() as connection:
        persisted = connection.execute(
            "SELECT COUNT(*) FROM governance_decision_results WHERE idempotency_key LIKE '%prefix%'"
        ).fetchone()
    assert persisted == (0,)
    for database_file in tmp_path.glob("governance.db*"):
        assert issued.raw_grant.encode() not in database_file.read_bytes()


def test_grant_insert_failure_rolls_back_new_snapshot(tmp_path: Path) -> None:
    store, _active, grants, decision_key = _fixture(tmp_path)
    with store.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER inject_apply_grant_failure
            BEFORE INSERT ON governance_apply_grants
            BEGIN
                SELECT RAISE(ABORT, 'injected grant failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected grant failure"):
        grants._issue_from_approved_decision(decision_key, authority_request=_request())
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_approved_snapshots"
        ).fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM governance_apply_grants").fetchone() == (0,)


def test_elapsed_grant_is_expired_before_reissue(tmp_path: Path) -> None:
    store, _active, grants, decision_key = _fixture(tmp_path)
    clock = [NOW]
    issuer = ApplyGrantService(
        store,
        grants.authority_service,
        grants.definitions,
        clock=lambda: clock[0],
    )
    first = issuer._issue_from_approved_decision(
        decision_key,
        authority_request=_request(),
        ttl=timedelta(seconds=1),
    )
    clock[0] += timedelta(seconds=2)
    second = issuer._issue_from_approved_decision(
        decision_key,
        authority_request=_request(),
        ttl=timedelta(seconds=1),
    )

    assert first.record.grant_id != second.record.grant_id
    assert issuer.get_grant(first.record.grant_id).state.value == "expired"
    assert issuer.get_grant(second.record.grant_id).state.value == "issued"
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_apply_grants WHERE state = 'issued'"
        ).fetchone() == (1,)


def test_snapshot_job_and_result_root_constraints_reject_malformed_identity(
    tmp_path: Path,
) -> None:
    store, _active, grants, decision_key = _fixture(tmp_path)
    issued = grants._issue_from_approved_decision(decision_key, authority_request=_request())
    snapshot = grants.get_snapshot(issued.record.snapshot_id)
    identity = (
        PROPOSAL.project_ref.namespace,
        PROPOSAL.project_ref.project_id,
        PROPOSAL.proposal_id,
    )
    with store.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO governance_approved_snapshots VALUES (
                    'APS-EVIL', ?, ?, ?, 'bad-definition', 'bad-snapshot',
                    1, 2, 1, 'NOTHEX', ?
                )
                """,
                (*identity, NOW.isoformat()),
            )
        connection.execute(
            """
            INSERT INTO governance_apply_jobs(
                job_id, snapshot_id, project_namespace, project_id, proposal_id,
                approved_snapshot_digest, expected_base_revision, status,
                attempts, fencing_token, lease_owner, lease_expires_at, retry_at,
                staged_artifact_digest, publish_request_digest, last_error_code,
                created_at, updated_at
            ) VALUES (
                'JOB-VALID', ?, ?, ?, ?, ?, ?, 'queued', 0, 0,
                NULL, NULL, NULL, NULL, NULL, NULL, ?, ?
            )
            """,
            (
                snapshot.snapshot_id,
                *identity,
                snapshot.snapshot_digest,
                snapshot.expected_base_revision,
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO governance_apply_request_results VALUES (
                    'apply-result-evil', ?, 'wrong/ns', 'wrong-project', ?,
                    ?, ?, 'JOB-VALID', 'ACT-APPLIER-1', 'human', ?,
                    'apply_requested', ?
                )
                """,
                (
                    hashlib.sha256(b"apply-result-evil").hexdigest(),
                    PROPOSAL.proposal_id,
                    snapshot.snapshot_id,
                    issued.record.grant_id,
                    CHANNEL.model_dump_json(exclude_none=True),
                    NOW.isoformat(),
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE governance_apply_jobs
                SET staged_artifact_digest = 'bad-digest'
                WHERE job_id = 'JOB-VALID'
                """
            )


def test_apply_request_atomically_creates_job_audit_and_two_outbox_events(
    tmp_path: Path,
) -> None:
    store, active, grants, decision_key = _fixture(tmp_path)
    issued = grants._issue_from_approved_decision(decision_key, authority_request=_request())
    service = ApplyRequestService(
        store,
        grants.authority_service,
        clock=lambda: NOW + timedelta(seconds=1),
    )
    key = "apply-request-1"
    fingerprint = hashlib.sha256(key.encode()).hexdigest()

    result = service.request_apply(
        PROPOSAL,
        authority_request=_request(),
        raw_grant=issued.raw_grant,
        idempotency_key=key,
        request_fingerprint=fingerprint,
    )

    assert result.proposal_status == "apply_requested"
    assert result.grant_id == issued.record.grant_id
    assert active.get(PROPOSAL).status.value == "apply_requested"
    assert grants.get_grant(issued.record.grant_id).state.value == "consumed"
    with store.connect() as connection:
        assert connection.execute(
            "SELECT status, attempts, fencing_token FROM governance_apply_jobs"
        ).fetchone() == ("queued", 0, 0)
        assert connection.execute(
            "SELECT grant_id, job_id FROM governance_apply_request_results"
        ).fetchone() == (issued.record.grant_id, result.job_id)
        audit = connection.execute(
            "SELECT event_type, before_state, after_state FROM governance_audit_events "
            "WHERE command_id LIKE 'apply:%'"
        ).fetchone()
        assert audit == ("proposal.apply_requested", "approved", "apply_requested")
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_outbox_events WHERE aggregate_sequence = 2"
        ).fetchone() == (2,)
    store.initialize()
    for database_file in tmp_path.glob("governance.db*"):
        assert issued.raw_grant.encode() not in database_file.read_bytes()


def test_apply_request_replay_returns_original_job_before_grant_state_validation(
    tmp_path: Path,
) -> None:
    store, _active, grants, decision_key = _fixture(tmp_path)
    issued = grants._issue_from_approved_decision(decision_key, authority_request=_request())
    service = ApplyRequestService(store, grants.authority_service, clock=lambda: NOW)
    key = "apply-replay-1"
    fingerprint = hashlib.sha256(key.encode()).hexdigest()
    first = service.request_apply(
        PROPOSAL,
        authority_request=_request(),
        raw_grant=issued.raw_grant,
        idempotency_key=key,
        request_fingerprint=fingerprint,
    )
    with store.connect() as connection:
        connection.execute(
            "UPDATE governance_actors SET status = 'disabled', updated_at = ? WHERE actor_id = ?",
            ((NOW + timedelta(seconds=1)).isoformat(), ACTOR.actor_id),
        )
    replay = service.request_apply(
        PROPOSAL,
        authority_request=_request(),
        raw_grant="",
        idempotency_key=key,
        request_fingerprint=fingerprint,
    )

    assert replay.replayed is True
    assert replay.job_id == first.job_id
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM governance_apply_jobs").fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_audit_events WHERE command_id LIKE 'apply:%'"
        ).fetchone() == (1,)


def test_apply_request_replay_fingerprint_conflict_has_no_side_effect(tmp_path: Path) -> None:
    store, _active, grants, decision_key = _fixture(tmp_path)
    issued = grants._issue_from_approved_decision(decision_key, authority_request=_request())
    service = ApplyRequestService(store, grants.authority_service, clock=lambda: NOW)
    key = "apply-conflict-1"
    service.request_apply(
        PROPOSAL,
        authority_request=_request(),
        raw_grant=issued.raw_grant,
        idempotency_key=key,
        request_fingerprint=hashlib.sha256(key.encode()).hexdigest(),
    )

    with pytest.raises(ApplyGovernanceError, match="IDEMPOTENCY_CONFLICT"):
        service.request_apply(
            PROPOSAL,
            authority_request=_request(),
            raw_grant=issued.raw_grant,
            idempotency_key=key,
            request_fingerprint=hashlib.sha256(b"different").hexdigest(),
        )
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM governance_apply_jobs").fetchone() == (1,)


def test_expired_apply_grant_does_not_mutate_proposal_or_create_job(tmp_path: Path) -> None:
    store, active, grants, decision_key = _fixture(tmp_path)
    issuer = ApplyGrantService(
        store, grants.authority_service, grants.definitions, clock=lambda: NOW
    )
    issued = issuer._issue_from_approved_decision(
        decision_key, authority_request=_request(), ttl=timedelta(seconds=1)
    )
    service = ApplyRequestService(
        store, grants.authority_service, clock=lambda: NOW + timedelta(seconds=1)
    )

    with pytest.raises(ApplyGovernanceError, match="APPLY_GRANT_EXPIRED"):
        service.request_apply(
            PROPOSAL,
            authority_request=_request(),
            raw_grant=issued.raw_grant,
            idempotency_key="apply-expired-1",
            request_fingerprint=hashlib.sha256(b"apply-expired-1").hexdigest(),
        )
    assert active.get(PROPOSAL).status.value == "approved"
    assert grants.get_grant(issued.record.grant_id).state.value == "issued"
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM governance_apply_jobs").fetchone() == (0,)


def test_wrong_channel_apply_grant_is_rejected_without_mutation(tmp_path: Path) -> None:
    store, active, grants, decision_key = _fixture(tmp_path)
    issued = grants._issue_from_approved_decision(decision_key, authority_request=_request())
    wrong_channel_request = DirectAuthorityRequest(
        provider=ChannelProvider.SLACK,
        provider_installation_ref="T123:APP1",
        external_actor_id="U123",
        project_ref=PROJECT,
        request_id="request-apply-wrong-channel",
        channel=ChannelRef(
            provider=ChannelProvider.SLACK,
            workspace_id="T123",
            channel_id="C999",
            message_id="1710000000.999999",
        ),
    )
    service = ApplyRequestService(store, grants.authority_service, clock=lambda: NOW)

    with pytest.raises(ApplyGovernanceError, match="APPLY_GRANT_CHANNEL_MISMATCH"):
        service.request_apply(
            PROPOSAL,
            authority_request=wrong_channel_request,
            raw_grant=issued.raw_grant,
            idempotency_key="apply-wrong-channel-1",
            request_fingerprint=hashlib.sha256(b"apply-wrong-channel-1").hexdigest(),
        )
    assert active.get(PROPOSAL).status.value == "approved"
    assert grants.get_grant(issued.record.grant_id).state.value == "issued"


def test_consumed_grant_with_new_key_and_action_token_are_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, include_action_token=True)
    store, _active, grants, decision_key, action_token = fixture
    issued = grants._issue_from_approved_decision(decision_key, authority_request=_request())
    service = ApplyRequestService(store, grants.authority_service, clock=lambda: NOW)
    key = "apply-consume-1"
    service.request_apply(
        PROPOSAL,
        authority_request=_request(),
        raw_grant=issued.raw_grant,
        idempotency_key=key,
        request_fingerprint=hashlib.sha256(key.encode()).hexdigest(),
    )

    with pytest.raises(ApplyGovernanceError, match="APPLY_GRANT_CONSUMED"):
        service.request_apply(
            PROPOSAL,
            authority_request=_request(),
            raw_grant=issued.raw_grant,
            idempotency_key="apply-consume-2",
            request_fingerprint=hashlib.sha256(b"apply-consume-2").hexdigest(),
        )
    with pytest.raises(ApplyGovernanceError, match="APPLY_GRANT_INVALID"):
        service.request_apply(
            PROPOSAL,
            authority_request=_request(),
            raw_grant=action_token,
            idempotency_key="apply-action-token-1",
            request_fingerprint=hashlib.sha256(b"apply-action-token-1").hexdigest(),
        )
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM governance_apply_jobs").fetchone() == (1,)


def test_apply_event_failure_rolls_back_proposal_grant_job_and_result(tmp_path: Path) -> None:
    store, active, grants, decision_key = _fixture(tmp_path)
    issued = grants._issue_from_approved_decision(decision_key, authority_request=_request())
    with store.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER inject_apply_audit_failure
            BEFORE INSERT ON governance_audit_events
            WHEN NEW.command_id LIKE 'apply:%'
            BEGIN SELECT RAISE(ABORT, 'injected apply audit failure'); END
            """
        )
    service = ApplyRequestService(store, grants.authority_service, clock=lambda: NOW)

    with pytest.raises(sqlite3.IntegrityError, match="injected apply audit failure"):
        service.request_apply(
            PROPOSAL,
            authority_request=_request(),
            raw_grant=issued.raw_grant,
            idempotency_key="apply-rollback-1",
            request_fingerprint=hashlib.sha256(b"apply-rollback-1").hexdigest(),
        )
    assert active.get(PROPOSAL).status.value == "approved"
    assert grants.get_grant(issued.record.grant_id).state.value == "issued"
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM governance_apply_jobs").fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_apply_request_results"
        ).fetchone() == (0,)


def test_concurrent_same_apply_request_creates_one_job_and_replays_loser(
    tmp_path: Path,
) -> None:
    store, _active, grants, decision_key = _fixture(tmp_path)
    issued = grants._issue_from_approved_decision(decision_key, authority_request=_request())
    service = ApplyRequestService(store, grants.authority_service, clock=lambda: NOW)
    barrier = threading.Barrier(2)
    key = "apply-concurrent-1"
    fingerprint = hashlib.sha256(key.encode()).hexdigest()

    def request():
        barrier.wait()
        return service.request_apply(
            PROPOSAL,
            authority_request=_request(),
            raw_grant=issued.raw_grant,
            idempotency_key=key,
            request_fingerprint=fingerprint,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _index: request(), range(2)))
    assert {result.replayed for result in results} == {False, True}
    assert results[0].job_id == results[1].job_id
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM governance_apply_jobs").fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_apply_request_results"
        ).fetchone() == (1,)


def test_second_apply_outbox_failure_rolls_back_entire_request(tmp_path: Path) -> None:
    store, active, grants, decision_key = _fixture(tmp_path)
    issued = grants._issue_from_approved_decision(decision_key, authority_request=_request())
    with store.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER inject_second_apply_outbox_failure
            BEFORE INSERT ON governance_outbox_events
            WHEN NEW.aggregate_sequence = 2 AND NEW.destination_ref LIKE 'provider:%'
            BEGIN SELECT RAISE(ABORT, 'injected second apply outbox failure'); END
            """
        )
    service = ApplyRequestService(store, grants.authority_service, clock=lambda: NOW)

    with pytest.raises(sqlite3.IntegrityError, match="injected second apply outbox failure"):
        service.request_apply(
            PROPOSAL,
            authority_request=_request(),
            raw_grant=issued.raw_grant,
            idempotency_key="apply-outbox-rollback-1",
            request_fingerprint=hashlib.sha256(b"apply-outbox-rollback-1").hexdigest(),
        )
    assert active.get(PROPOSAL).status.value == "approved"
    assert grants.get_grant(issued.record.grant_id).state.value == "issued"
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM governance_apply_jobs").fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_apply_request_results"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_audit_events WHERE command_id LIKE 'apply:%'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_outbox_events WHERE aggregate_sequence = 2"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT aggregate_sequence FROM governance_aggregate_sequences"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT MIN(next_sequence), MAX(next_sequence) FROM governance_outbox_destinations"
        ).fetchone() == (2, 2)


def test_startup_rejects_orphan_apply_job_without_result(tmp_path: Path) -> None:
    store, _active, grants, decision_key = _fixture(tmp_path)
    issued = grants._issue_from_approved_decision(decision_key, authority_request=_request())
    snapshot = grants.get_snapshot(issued.record.snapshot_id)
    with store.connect() as connection:
        connection.execute(
            """
            INSERT INTO governance_apply_jobs(
                job_id, snapshot_id, project_namespace, project_id, proposal_id,
                approved_snapshot_digest, expected_base_revision, status,
                attempts, fencing_token, lease_owner, lease_expires_at, retry_at,
                staged_artifact_digest, publish_request_digest, last_error_code,
                created_at, updated_at
            ) VALUES (
                'JOB-ORPHAN', ?, ?, ?, ?, ?, ?, 'queued', 0, 0,
                NULL, NULL, NULL, NULL, NULL, NULL, ?, ?
            )
            """,
            (
                snapshot.snapshot_id,
                PROPOSAL.project_ref.namespace,
                PROPOSAL.project_ref.project_id,
                PROPOSAL.proposal_id,
                snapshot.snapshot_digest,
                snapshot.expected_base_revision,
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )

    with pytest.raises(GovernanceEventError, match="APPLY_RESULT_ROOT_MISMATCH"):
        store.initialize()


def test_startup_rejects_approved_snapshot_without_grant_root(tmp_path: Path) -> None:
    store, _active, _grants, _decision_key = _fixture(tmp_path)
    with store.connect() as connection:
        connection.execute(
            """
            INSERT INTO governance_approved_snapshots(
                snapshot_id, project_namespace, project_id, proposal_id,
                definition_digest, snapshot_digest, content_revision,
                state_revision, decision_epoch, expected_base_revision, approved_at
            ) VALUES (
                'APS-ORPHAN', ?, ?, ?, ?, ?, 99, 99, 99, 'bbbbbbb', ?
            )
            """,
            (
                PROPOSAL.project_ref.namespace,
                PROPOSAL.project_ref.project_id,
                PROPOSAL.proposal_id,
                f"sha256:{'d' * 64}",
                f"sha256:{'e' * 64}",
                NOW.isoformat(),
            ),
        )

    with pytest.raises(GovernanceEventError, match="APPLY_RESULT_ROOT_MISMATCH"):
        store.initialize()


def test_apply_job_lease_reclaim_increments_fence_and_rejects_stale_worker(
    tmp_path: Path,
) -> None:
    store, job_id = _queued_job_fixture(tmp_path)
    clock = [NOW]
    jobs = ApplyJobService(store, clock=lambda: clock[0])
    first = jobs.claim_next("worker-1", lease_ttl=timedelta(seconds=2))
    assert first is not None
    assert first.job_id == job_id
    assert first.attempts == 1
    assert first.fencing_token == 1
    running = jobs.start(job_id, worker_id="worker-1", fencing_token=1)
    assert running.status.value == "running"
    heartbeat = jobs.heartbeat(
        job_id,
        worker_id="worker-1",
        fencing_token=1,
        lease_ttl=timedelta(seconds=1),
    )
    assert heartbeat.lease_expires_at == first.lease_expires_at

    clock[0] += timedelta(seconds=3)
    second = jobs.claim_next("worker-2", lease_ttl=timedelta(seconds=5))
    assert second is not None
    assert second.job_id == job_id
    assert second.attempts == 2
    assert second.fencing_token == 2
    with pytest.raises(ApplyGovernanceError, match="APPLY_JOB_FENCE_STALE"):
        jobs.heartbeat(job_id, worker_id="worker-1", fencing_token=1)
    with pytest.raises(ApplyGovernanceError, match="APPLY_JOB_FENCE_STALE"):
        jobs.stage_for_publish(
            job_id,
            worker_id="worker-1",
            fencing_token=1,
            artifact_bytes=b"stale artifact",
            publish_request_bytes=b"stale publish input",
        )


def test_apply_job_retry_wait_reclaims_only_after_due_time(tmp_path: Path) -> None:
    store, job_id = _queued_job_fixture(tmp_path)
    clock = [NOW]
    jobs = ApplyJobService(store, clock=lambda: clock[0])
    claimed = jobs.claim_next("worker-1")
    assert claimed is not None
    retry = jobs.schedule_retry(
        job_id,
        worker_id="worker-1",
        fencing_token=claimed.fencing_token,
        retry_delay=timedelta(seconds=10),
        error_code="STAGING_TEMPORARY_FAILURE",
    )
    assert retry.status.value == "retry_wait"
    assert jobs.claim_next("worker-2") is None

    clock[0] += timedelta(seconds=10)
    reclaimed = jobs.claim_next("worker-2")
    assert reclaimed is not None
    assert reclaimed.fencing_token == claimed.fencing_token + 1
    assert reclaimed.attempts == 2


def test_apply_job_atomically_stages_publish_input_without_canonical_write(
    tmp_path: Path,
) -> None:
    store, job_id = _queued_job_fixture(tmp_path)
    jobs = ApplyJobService(store, clock=lambda: NOW)
    claimed = jobs.claim_next("worker-1")
    assert claimed is not None
    jobs.start(job_id, worker_id="worker-1", fencing_token=claimed.fencing_token)
    publish = jobs.stage_for_publish(
        job_id,
        worker_id="worker-1",
        fencing_token=claimed.fencing_token,
        artifact_bytes=b"staged patch bytes",
        publish_request_bytes=b'{"action":"publish"}',
    )
    assert publish.status.value == "publish_pending"
    assert publish.lease_owner is None
    assert publish.staged_artifact_digest == (
        f"sha256:{hashlib.sha256(b'staged patch bytes').hexdigest()}"
    )
    expected_publish_digest = hashlib.sha256(b'{"action":"publish"}').hexdigest()
    assert publish.publish_request_digest == f"sha256:{expected_publish_digest}"
    with store.connect() as connection:
        assert connection.execute(
            "SELECT artifact_bytes FROM governance_staging_artifacts"
        ).fetchone() == (b"staged patch bytes",)
        assert connection.execute(
            "SELECT publish_request_bytes FROM governance_publish_inputs"
        ).fetchone() == (b'{"action":"publish"}',)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_apply_job_events"
        ).fetchone() == (3,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_audit_events WHERE event_type LIKE 'apply_job.%'"
        ).fetchone() == (3,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_outbox_events WHERE aggregate_sequence >= 3"
        ).fetchone() == (6,)
    store.initialize()


def test_concurrent_apply_job_claim_has_one_winner(tmp_path: Path) -> None:
    store, job_id = _queued_job_fixture(tmp_path)
    jobs = ApplyJobService(store, clock=lambda: NOW)
    barrier = threading.Barrier(2)

    def claim(worker: str):
        barrier.wait()
        return jobs.claim_next(worker)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(claim, ("worker-1", "worker-2")))
    winners = tuple(result for result in results if result is not None)
    assert len(winners) == 1
    assert winners[0].job_id == job_id
    assert winners[0].fencing_token == 1


def test_apply_job_event_failure_rolls_back_claim_and_outbox(tmp_path: Path) -> None:
    store, job_id = _queued_job_fixture(tmp_path)
    with store.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER inject_job_audit_failure
            BEFORE INSERT ON governance_audit_events
            WHEN NEW.event_type = 'apply_job.claimed'
            BEGIN SELECT RAISE(ABORT, 'injected job audit failure'); END
            """
        )
    jobs = ApplyJobService(store, clock=lambda: NOW)

    with pytest.raises(sqlite3.IntegrityError, match="injected job audit failure"):
        jobs.claim_next("worker-1")
    job = jobs.get_job(job_id)
    assert job.status.value == "queued"
    assert job.attempts == 0
    assert job.fencing_token == 0
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_apply_job_events"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_audit_events WHERE event_type LIKE 'apply_job.%'"
        ).fetchone() == (0,)


def test_startup_rejects_apply_job_counter_regression(tmp_path: Path) -> None:
    store, job_id = _queued_job_fixture(tmp_path)
    jobs = ApplyJobService(store, clock=lambda: NOW)
    claimed = jobs.claim_next("worker-1")
    assert claimed is not None
    with store.connect() as connection:
        connection.execute(
            "UPDATE governance_apply_jobs SET attempts = 0, fencing_token = 0 WHERE job_id = ?",
            (job_id,),
        )

    with pytest.raises(GovernanceEventError, match="APPLY_JOB_EVENT_ROOT_MISMATCH"):
        store.initialize()


def test_startup_rejects_unrooted_queued_job_lifecycle_fields(tmp_path: Path) -> None:
    store, job_id = _queued_job_fixture(tmp_path)
    with store.connect() as connection:
        connection.execute(
            """
            UPDATE governance_apply_jobs
            SET attempts = 3, fencing_token = 3,
                staged_artifact_digest = ?, last_error_code = 'TAMPERED'
            WHERE job_id = ?
            """,
            (f"sha256:{'a' * 64}", job_id),
        )

    with pytest.raises(GovernanceEventError, match="APPLY_JOB_EVENT_ROOT_MISMATCH"):
        store.initialize()


def test_retry_attempt_cap_converges_to_dead_letter(tmp_path: Path) -> None:
    store, job_id = _queued_job_fixture(tmp_path)
    jobs = ApplyJobService(store, clock=lambda: NOW, max_attempts=1)
    claimed = jobs.claim_next("worker-1")
    assert claimed is not None
    result = jobs.schedule_retry(
        job_id,
        worker_id="worker-1",
        fencing_token=claimed.fencing_token,
        retry_delay=timedelta(seconds=1),
        error_code="NON_RECOVERABLE",
    )
    assert result.status.value == "dead_letter"
    assert result.retry_at is None
    assert jobs.claim_next("worker-2") is None


def test_startup_rejects_corrupt_staging_artifact_bytes(tmp_path: Path) -> None:
    store, job_id = _queued_job_fixture(tmp_path)
    jobs = ApplyJobService(store, clock=lambda: NOW)
    claimed = jobs.claim_next("worker-1")
    assert claimed is not None
    jobs.start(job_id, worker_id="worker-1", fencing_token=claimed.fencing_token)
    jobs.stage_for_publish(
        job_id,
        worker_id="worker-1",
        fencing_token=claimed.fencing_token,
        artifact_bytes=b"original artifact",
        publish_request_bytes=b"original publish input",
    )
    trigger_sql = next(
        statement
        for statement in INITIAL_MIGRATIONS[10].statements
        if "CREATE TRIGGER governance_staging_artifacts_no_update" in statement
    )
    with store.connect() as connection:
        connection.execute("DROP TRIGGER governance_staging_artifacts_no_update")
        connection.execute(
            "UPDATE governance_staging_artifacts SET artifact_bytes = ? WHERE job_id = ?",
            (b"corrupt artifact", job_id),
        )
        connection.execute(trigger_sql)

    with pytest.raises(GovernanceEventError, match="APPLY_ARTIFACT_ROOT_MISMATCH"):
        store.initialize()


def test_claim_samples_clock_after_waiting_for_database_write_lock(tmp_path: Path) -> None:
    store, _job_id = _queued_job_fixture(tmp_path)
    clock = [NOW]
    jobs = ApplyJobService(store, clock=lambda: clock[0])
    with ThreadPoolExecutor(max_workers=1) as executor:
        with store.connect() as connection, governance_transaction(connection):
            future = executor.submit(
                lambda: jobs.claim_next("worker-after-lock", lease_ttl=timedelta(seconds=5))
            )
            threading.Event().wait(0.05)
            assert not future.done()
            clock[0] += timedelta(seconds=10)
        claimed = future.result(timeout=5)
    assert claimed is not None
    assert claimed.lease_expires_at == NOW + timedelta(seconds=15)


def test_expired_lease_at_attempt_cap_dead_letters_without_reclaim(tmp_path: Path) -> None:
    store, job_id = _queued_job_fixture(tmp_path)
    clock = [NOW]
    jobs = ApplyJobService(store, clock=lambda: clock[0], max_attempts=1)
    claimed = jobs.claim_next("worker-1", lease_ttl=timedelta(seconds=1))
    assert claimed is not None
    clock[0] += timedelta(seconds=1)

    assert jobs.claim_next("worker-2") is None
    dead = jobs.get_job(job_id)
    assert dead.status.value == "dead_letter"
    assert dead.attempts == 1
    assert dead.fencing_token == 1
    with store.connect() as connection:
        assert connection.execute(
            "SELECT event_type, worker_id FROM governance_apply_job_events "
            "ORDER BY event_sequence DESC LIMIT 1"
        ).fetchone() == ("dead_lettered", "system-recovery")


def test_claim_prioritizes_runnable_job_over_older_exhausted_job(tmp_path: Path) -> None:
    store, first_job_id = _queued_job_fixture(tmp_path)
    clock = [NOW]
    jobs = ApplyJobService(store, clock=lambda: clock[0], max_attempts=1)
    first = jobs.claim_next("worker-1", lease_ttl=timedelta(seconds=1))
    assert first is not None
    second_proposal_id = "PROP-20260730-BBBBBBBB"
    definition_digest = f"sha256:{'c' * 64}"
    snapshot_digest = f"sha256:{'d' * 64}"
    with store.connect() as connection:
        connection.execute(
            """
            INSERT INTO governance_active_proposals VALUES (
                ?, ?, ?, ?, 1, 3, 1, 'apply_requested', ?, ?
            )
            """,
            (
                PROJECT.namespace,
                PROJECT.project_id,
                second_proposal_id,
                definition_digest,
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO governance_approved_snapshots VALUES (
                'APS-SECOND', ?, ?, ?, ?, ?, 1, 2, 1, 'bbbbbbb', ?
            )
            """,
            (
                PROJECT.namespace,
                PROJECT.project_id,
                second_proposal_id,
                definition_digest,
                snapshot_digest,
                NOW.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO governance_apply_jobs VALUES (
                'JOB-SECOND', 'APS-SECOND', ?, ?, ?, ?, 'bbbbbbb', 'queued',
                0, 0, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?
            )
            """,
            (
                PROJECT.namespace,
                PROJECT.project_id,
                second_proposal_id,
                snapshot_digest,
                (NOW + timedelta(seconds=1)).isoformat(),
                (NOW + timedelta(seconds=1)).isoformat(),
            ),
        )
    clock[0] += timedelta(seconds=1)

    claimed = jobs.claim_next("worker-2")
    assert claimed is not None
    assert claimed.job_id == "JOB-SECOND"
    assert jobs.get_job(first_job_id).status.value == "leased"


def test_apply_job_outbox_dispatches_on_monotonic_job_sequence(tmp_path: Path) -> None:
    class StrictRemote:
        def __init__(self, destination_ref: str) -> None:
            self.destination_ref = destination_ref
            self.last_revision = 0

        def reconcile(self, _event):
            return None

        def send(self, event):
            assert event.source_state_revision > self.last_revision
            self.last_revision = event.source_state_revision
            return f"remote:{event.event_id}"

    store, job_id = _queued_job_fixture(tmp_path)
    jobs = ApplyJobService(store, clock=lambda: NOW)
    claimed = jobs.claim_next("worker-1")
    assert claimed is not None
    jobs.start(job_id, worker_id="worker-1", fencing_token=claimed.fencing_token)
    jobs.heartbeat(job_id, worker_id="worker-1", fencing_token=claimed.fencing_token)
    jobs.stage_for_publish(
        job_id,
        worker_id="worker-1",
        fencing_token=claimed.fencing_token,
        artifact_bytes=b"dispatch artifact",
        publish_request_bytes=b"dispatch publish input",
    )
    with store.connect() as connection:
        destinations = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT destination_ref FROM governance_outbox_destinations "
                "WHERE destination_ref LIKE ? ORDER BY destination_ref",
                (f"apply-job:{job_id}:%",),
            ).fetchall()
        )
    assert len(destinations) == 2
    dispatcher = OutboxDispatcher(store, clock=lambda: NOW)
    for destination_ref in destinations:
        remote = StrictRemote(destination_ref)
        while True:
            delivered = dispatcher.deliver_next("dispatcher-1", remote)
            if delivered is None:
                break
            assert delivered.state is OutboxState.DELIVERED
        assert remote.last_revision >= 1


def test_stage_second_outbox_failure_rolls_back_artifacts_and_lifecycle(tmp_path: Path) -> None:
    store, job_id = _queued_job_fixture(tmp_path)
    jobs = ApplyJobService(store, clock=lambda: NOW)
    claimed = jobs.claim_next("worker-1")
    assert claimed is not None
    jobs.start(job_id, worker_id="worker-1", fencing_token=claimed.fencing_token)
    with store.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER inject_stage_observer_outbox_failure
            BEFORE INSERT ON governance_outbox_events
            WHEN NEW.destination_ref LIKE 'apply-job:%:observer'
              AND NEW.source_state_revision = 3
            BEGIN SELECT RAISE(ABORT, 'injected stage observer failure'); END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected stage observer failure"):
        jobs.stage_for_publish(
            job_id,
            worker_id="worker-1",
            fencing_token=claimed.fencing_token,
            artifact_bytes=b"rollback artifact",
            publish_request_bytes=b"rollback publish input",
        )
    current = jobs.get_job(job_id)
    assert current.status.value == "running"
    assert current.staged_artifact_digest is None
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_staging_artifacts"
        ).fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM governance_publish_inputs").fetchone() == (
            0,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_apply_job_events"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_audit_events WHERE event_type LIKE 'apply_job.%'"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_outbox_events WHERE destination_ref LIKE 'apply-job:%'"
        ).fetchone() == (4,)


def test_stage_rejects_oversized_artifact_before_persistence(tmp_path: Path) -> None:
    store, job_id = _queued_job_fixture(tmp_path)
    jobs = ApplyJobService(store, clock=lambda: NOW)
    claimed = jobs.claim_next("worker-1")
    assert claimed is not None
    jobs.start(job_id, worker_id="worker-1", fencing_token=claimed.fencing_token)

    with pytest.raises(ValueError, match="4 MiB"):
        jobs.stage_for_publish(
            job_id,
            worker_id="worker-1",
            fencing_token=claimed.fencing_token,
            artifact_bytes=b"x" * (ApplyJobService.MAX_ROOT_BYTES + 1),
            publish_request_bytes=b"publish",
        )
    assert jobs.get_job(job_id).status.value == "running"
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_staging_artifacts"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    ("column", "value"),
    (("lease_owner", "evil-worker"), ("last_error_code", "EVIL_ERROR")),
)
def test_startup_rejects_unrooted_job_field_tamper(
    tmp_path: Path,
    column: str,
    value: str,
) -> None:
    store, job_id = _queued_job_fixture(tmp_path)
    jobs = ApplyJobService(store, clock=lambda: NOW)
    assert jobs.claim_next("worker-1") is not None
    with store.connect() as connection:
        connection.execute(
            f"UPDATE governance_apply_jobs SET {column} = ? WHERE job_id = ?",
            (value, job_id),
        )

    with pytest.raises(GovernanceEventError, match="APPLY_JOB_EVENT_ROOT_MISMATCH"):
        store.initialize()


def test_publish_intent_is_relationally_rooted_and_immutable(tmp_path: Path) -> None:
    store, job = _publish_pending_job_fixture(tmp_path)
    intent_id = _insert_prepared_publish_foundation(store, job.job_id)

    with store.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="publish intent identity is immutable"):
            connection.execute(
                "UPDATE governance_publish_intents SET candidate_commit = ? WHERE intent_id = ?",
                ("d" * 40, intent_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="publish intent is durable"):
            connection.execute(
                "DELETE FROM governance_publish_intents WHERE intent_id = ?",
                (intent_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO governance_publish_intents(
                    intent_id, job_id, snapshot_id, project_namespace, project_id,
                    proposal_id, fencing_token, approved_snapshot_digest,
                    expected_base_revision, staged_artifact_digest, publish_request_digest,
                    canonical_ref, expected_old_ref, candidate_commit,
                    candidate_tree_digest, status, prepared_at
                )
                SELECT 'PBI-0000000000000002', job_id, snapshot_id, project_namespace,
                       project_id, proposal_id, fencing_token,
                       approved_snapshot_digest, expected_base_revision,
                       ?, publish_request_digest, canonical_ref, expected_old_ref,
                       candidate_commit, candidate_tree_digest, 'prepared', prepared_at
                FROM governance_publish_intents WHERE intent_id = ?
                """,
                (f"sha256:{'f' * 64}", intent_id),
            )


def test_publish_gate_requires_monotonic_revision_and_explicit_release(tmp_path: Path) -> None:
    store, job = _publish_pending_job_fixture(tmp_path)
    intent_id = _insert_prepared_publish_foundation(store, job.job_id)

    with store.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="publish gate"):
            connection.execute(
                """
                UPDATE governance_project_publish_gates
                SET state = 'unlocked', active_intent_id = NULL
                WHERE project_namespace = ? AND project_id = ?
                """,
                (PROJECT.namespace, PROJECT.project_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="transition is inconsistent"):
            connection.execute(
                """
                UPDATE governance_project_publish_gates
                SET state = 'unlocked', active_intent_id = NULL,
                    gate_revision = gate_revision + 1, updated_at = ?
                WHERE project_namespace = ? AND project_id = ?
                """,
                (NOW.isoformat(), PROJECT.namespace, PROJECT.project_id),
            )
        connection.execute(
            """
            UPDATE governance_publish_intents
            SET status = 'cancelled', resolved_at = ?
            WHERE intent_id = ?
            """,
            (NOW.isoformat(), intent_id),
        )
        connection.execute(
            """
            UPDATE governance_project_publish_gates
            SET state = 'unlocked', active_intent_id = NULL,
                gate_revision = gate_revision + 1, updated_at = ?
            WHERE project_namespace = ? AND project_id = ?
            """,
            (NOW.isoformat(), PROJECT.namespace, PROJECT.project_id),
        )
        with pytest.raises(sqlite3.IntegrityError, match="publish gate is durable"):
            connection.execute(
                """
                DELETE FROM governance_project_publish_gates
                WHERE project_namespace = ? AND project_id = ?
                """,
                (PROJECT.namespace, PROJECT.project_id),
            )
        gate = connection.execute(
            """
            SELECT active_intent_id, gate_revision, state
            FROM governance_project_publish_gates
            """
        ).fetchone()
    assert gate == (None, 2, "unlocked")
    assert intent_id == "PBI-0000000000000001"


def test_publish_result_is_correlated_terminal_evidence(tmp_path: Path) -> None:
    store, job = _publish_pending_job_fixture(tmp_path)
    intent_id = _insert_prepared_publish_foundation(store, job.job_id)

    with store.connect() as connection:
        connection.execute(
            """
            UPDATE governance_publish_intents
            SET status = 'published', resolved_at = ?
            WHERE intent_id = ?
            """,
            (NOW.isoformat(), intent_id),
        )
        connection.execute(
            """
            INSERT INTO governance_publish_results(
                intent_id, job_id, snapshot_id, project_namespace, project_id,
                proposal_id, fencing_token, expected_old_ref, candidate_commit,
                actual_ref, outcome, error_code, resolved_at
            )
            SELECT intent_id, job_id, snapshot_id, project_namespace, project_id,
                   proposal_id, fencing_token, expected_old_ref, candidate_commit,
                   candidate_commit, 'published', NULL, ?
            FROM governance_publish_intents WHERE intent_id = ?
            """,
            (NOW.isoformat(), intent_id),
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE governance_publish_results SET actual_ref = ? WHERE intent_id = ?",
                ("e" * 40, intent_id),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO governance_publish_results(
                    intent_id, job_id, snapshot_id, project_namespace, project_id,
                    proposal_id, fencing_token, expected_old_ref, candidate_commit,
                    actual_ref, outcome, error_code, resolved_at
                ) VALUES (
                    'PBI-0000000000000002', 'JOB-MISSING', 'SNP-MISSING',
                    ?, ?, ?, 1, ?, ?, ?, 'published', NULL, ?
                )
                """,
                (
                    PROJECT.namespace,
                    PROJECT.project_id,
                    PROPOSAL.proposal_id,
                    "a" * 40,
                    "b" * 40,
                    "b" * 40,
                    NOW.isoformat(),
                ),
            )


def test_publish_gate_model_rejects_unsafe_canonical_ref() -> None:
    with pytest.raises(ValidationError):
        ProjectPublishGateView(
            project_ref=PROJECT,
            canonical_ref="refs/heads/main..evil",
            active_intent_id=None,
            gate_revision=1,
            state=PublishGateState.UNLOCKED,
            updated_at=NOW,
        )
