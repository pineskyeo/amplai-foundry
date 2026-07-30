from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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
    CandidateCommitEvidence,
    ChannelProvider,
    ChannelRef,
    DecisionAction,
    DecisionError,
    DecisionService,
    DirectAuthorityRequest,
    FencedGitPublishCoordinator,
    FencedGitPublishWorkflow,
    GitCASOutcome,
    GitPublishAmbiguousError,
    ImmutableDefinitionObjectStore,
    OutboxDispatcher,
    OutboxState,
    ProposalDefinitionManifest,
    ProposalRef,
    ProposalSubmissionService,
    PublishGovernanceError,
    PublishPreparationService,
    PublishResolutionService,
    PublishResolutionType,
    SubprocessGitCandidateInspector,
    canonicalize_definition,
)
from amplai_foundry.governance.apply_jobs import (
    ApplyGovernanceError,
    ApplyGrantService,
    ApplyJobService,
    ApplyRequestService,
)
from amplai_foundry.governance.events import GovernanceEventError
from amplai_foundry.governance.migrations import INITIAL_MIGRATIONS, MigrationRunner
from amplai_foundry.governance.publish import ProjectPublishGateView, PublishGateState
from amplai_foundry.governance.store import (
    GovernanceCommitAmbiguousError,
    GovernanceStore,
    GovernanceStoreError,
    governance_transaction,
)

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


def _hard_kill_before_cas(
    database_path: str,
    repository_path: str,
    intent_id: str,
    marker_path: str,
) -> None:
    coordinator = FencedGitPublishCoordinator(
        GovernanceStore(Path(database_path)),
        Path(repository_path),
        coordinator_id="hard-kill-before-cas",
    )

    def block_candidate(*_args, **_kwargs):
        Path(marker_path).write_text("claimed", encoding="utf-8")
        time.sleep(30)

    coordinator._backend.inspect_candidate = block_candidate
    coordinator.publish_prepared_ref(intent_id)


def _hard_kill_after_cas(
    database_path: str,
    repository_path: str,
    intent_id: str,
    marker_path: str,
) -> None:
    coordinator = FencedGitPublishCoordinator(
        GovernanceStore(Path(database_path)),
        Path(repository_path),
        coordinator_id="hard-kill-after-cas",
    )
    coordinator.publish_prepared_ref(intent_id)
    Path(marker_path).write_text("published", encoding="utf-8")
    os._exit(73)


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
    base_revision: str = "a13d92f",
    migration_runner: MigrationRunner | None = None,
):
    store = GovernanceStore(
        tmp_path / "governance.db",
        migration_runner=migration_runner,
    )
    store.initialize()
    objects = ImmutableDefinitionObjectStore(PROJECT, tmp_path)
    canonical = canonicalize_definition(
        ProposalDefinitionManifest(
            proposal_ref=PROPOSAL,
            operations=({"marker": "apply", "type": "CREATE"},),
            base_revision=base_revision,
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


def _queued_job_fixture(
    tmp_path: Path,
    *,
    base_revision: str = "a13d92f",
    migration_runner: MigrationRunner | None = None,
):
    store, _active, grants, decision_key = _fixture(
        tmp_path,
        base_revision=base_revision,
        migration_runner=migration_runner,
    )
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


def _publish_pending_job_fixture(
    tmp_path: Path,
    *,
    artifact_bytes: bytes = b"immutable staged tree",
    publish_request_bytes: bytes = b'{"canonical_ref":"refs/heads/main"}',
    base_revision: str = "a13d92f",
    migration_runner: MigrationRunner | None = None,
):
    store, job_id = _queued_job_fixture(
        tmp_path,
        base_revision=base_revision,
        migration_runner=migration_runner,
    )
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
        artifact_bytes=artifact_bytes,
        publish_request_bytes=publish_request_bytes,
    )
    return store, published


class FakePublishGit:
    def __init__(
        self,
        *,
        current_ref: str = "a13d92f" + "a" * 33,
        candidate_commit: str = "b" * 40,
        parent_commit: str | None = None,
    ) -> None:
        self.current_ref = current_ref
        self.candidate_commit = candidate_commit
        self.parent_commit = parent_commit or current_ref
        self.inspected: tuple[bytes, bytes] | None = None
        self.read_count = 0

    def read_ref(self, canonical_ref: str) -> str:
        self.read_count += 1
        assert canonical_ref == "refs/heads/main"
        return self.current_ref

    def inspect_candidate(
        self,
        candidate_commit: str,
        *,
        artifact_bytes: bytes,
        publish_request_bytes: bytes,
    ) -> CandidateCommitEvidence:
        assert candidate_commit == self.candidate_commit
        self.inspected = (artifact_bytes, publish_request_bytes)
        return CandidateCommitEvidence(
            candidate_commit=candidate_commit,
            parent_commit=self.parent_commit,
            candidate_tree_digest=f"sha256:{hashlib.sha256(artifact_bytes).hexdigest()}",
            canonical_ref="refs/heads/main",
        )


def _insert_prepared_publish_foundation(
    store: GovernanceStore,
    job_id: str,
    *,
    canonical_ref: str = "refs/heads/main",
    create_gate: bool = True,
) -> str:
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
                canonical_ref,
                "a13d92f" + "a" * 33,
                "b" * 40,
                f"sha256:{'c' * 64}",
                NOW.isoformat(),
            ),
        )
        if create_gate:
            connection.execute(
                """
                INSERT INTO governance_project_publish_gates(
                    project_namespace, project_id, canonical_ref, active_intent_id,
                    gate_revision, state, updated_at
                ) VALUES (?, ?, ?, ?, 1, 'locked', ?)
                """,
                (
                    PROJECT.namespace,
                    PROJECT.project_id,
                    canonical_ref,
                    intent_id,
                    NOW.isoformat(),
                ),
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
                    ?, ?, ?, ?, 1, 3, 1, 'apply_requested', ?, ?, NULL
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


def test_populated_v12_publish_roots_upgrade_to_v13_without_rewrite(tmp_path: Path) -> None:
    legacy_runner = MigrationRunner(INITIAL_MIGRATIONS[:12])
    store, job = _publish_pending_job_fixture(
        tmp_path,
        migration_runner=legacy_runner,
    )
    intent_id = _insert_prepared_publish_foundation(store, job.job_id)
    with store.connect() as connection:
        before_intent = connection.execute(
            "SELECT * FROM governance_publish_intents WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
        before_gate = connection.execute(
            "SELECT * FROM governance_project_publish_gates WHERE active_intent_id = ?",
            (intent_id,),
        ).fetchone()
        assert connection.execute(
            "SELECT MAX(version) FROM governance_schema_migrations"
        ).fetchone() == (12,)

    upgraded = GovernanceStore(
        tmp_path / "governance.db",
        migration_runner=MigrationRunner(INITIAL_MIGRATIONS[:13]),
    )

    assert upgraded.initialize().schema_version == 13
    assert upgraded.check_startup().healthy
    with upgraded.connect() as connection:
        assert (
            connection.execute(
                "SELECT * FROM governance_publish_intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            == before_intent
        )
        assert (
            connection.execute(
                "SELECT * FROM governance_project_publish_gates WHERE active_intent_id = ?",
                (intent_id,),
            ).fetchone()
            == before_gate
        )
        assert connection.execute("SELECT COUNT(*) FROM governance_publish_claims").fetchone() == (
            0,
        )
        connection.execute(
            """
            INSERT INTO governance_publish_claims(
                claim_id, intent_id, project_namespace, project_id,
                coordinator_id, claim_fencing_token, state, claimed_at, resolved_at
            ) VALUES
                ('PCL-0000000000000001', ?, ?, ?, 'legacy-released', 1,
                 'released', '2026-07-30T11:00:00Z', '2026-07-30T11:01:00Z'),
                ('PCL-0000000000000002', ?, ?, ?, 'legacy-active', 2,
                 'active', '2026-07-30T11:02:00Z', NULL)
            """,
            (
                intent_id,
                PROJECT.namespace,
                PROJECT.project_id,
                intent_id,
                PROJECT.namespace,
                PROJECT.project_id,
            ),
        )
    latest = GovernanceStore(tmp_path / "governance.db")
    assert latest.initialize().schema_version == len(INITIAL_MIGRATIONS)
    assert latest.check_startup().healthy
    with latest.connect() as connection:
        assert (
            connection.execute(
                "SELECT * FROM governance_publish_intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            == before_intent
        )
        assert (
            connection.execute(
                "SELECT * FROM governance_project_publish_gates WHERE active_intent_id = ?",
                (intent_id,),
            ).fetchone()
            == before_gate
        )
        assert connection.execute(
            """
            SELECT claim_id, claim_fencing_token, state
            FROM governance_publish_claims ORDER BY claim_fencing_token
            """
        ).fetchall() == [
            ("PCL-0000000000000001", 1, "released"),
            ("PCL-0000000000000002", 2, "active"),
        ]
        assert connection.execute(
            "SELECT applied_revision FROM governance_active_proposals"
        ).fetchone() == (None,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_publish_resolution_events"
        ).fetchone() == (0,)


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


@pytest.mark.parametrize(
    "canonical_ref",
    (
        "refs/heads/main evil",
        "refs/heads/main~evil",
        "refs/heads/main?evil",
        "refs/heads/main[evil",
        "refs/heads/main\\evil",
        "refs/heads/main\tevil",
        "refs/heads/main.",
        "refs/heads/.hidden",
        "refs/heads/release.lock",
        "refs/heads/group/.hidden",
        "refs/heads/a/\x01b",
        "refs/heads/a/\x7fb",
    ),
)
def test_publish_persistence_rejects_git_invalid_canonical_refs(
    tmp_path: Path,
    canonical_ref: str,
) -> None:
    assert (
        subprocess.run(
            ("git", "check-ref-format", canonical_ref),
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )
    store, job = _publish_pending_job_fixture(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_prepared_publish_foundation(
            store,
            job.job_id,
            canonical_ref=canonical_ref,
        )
    with store.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO governance_project_publish_gates(
                project_namespace, project_id, canonical_ref, active_intent_id,
                gate_revision, state, updated_at
            ) VALUES (?, ?, ?, NULL, 1, 'unlocked', ?)
            """,
            (PROJECT.namespace, PROJECT.project_id, canonical_ref, NOW.isoformat()),
        )
    with pytest.raises(ValidationError):
        ProjectPublishGateView(
            project_ref=PROJECT,
            canonical_ref=canonical_ref,
            active_intent_id=None,
            gate_revision=1,
            state=PublishGateState.UNLOCKED,
            updated_at=NOW,
        )


def test_publish_result_rejects_prepared_intent_contradiction(tmp_path: Path) -> None:
    store, job = _publish_pending_job_fixture(tmp_path)
    intent_id = _insert_prepared_publish_foundation(store, job.job_id)
    with (
        store.connect() as connection,
        pytest.raises(
            sqlite3.IntegrityError,
            match="does not match terminal intent",
        ),
    ):
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


def test_startup_rejects_prepared_publish_intent_without_gate(tmp_path: Path) -> None:
    store, job = _publish_pending_job_fixture(tmp_path)
    _insert_prepared_publish_foundation(store, job.job_id, create_gate=False)

    with pytest.raises(GovernanceEventError, match="PUBLISH_INTENT_ROOT_MISMATCH"):
        store.check_startup()


def test_startup_rejects_terminal_publish_intent_without_result(tmp_path: Path) -> None:
    store, job = _publish_pending_job_fixture(tmp_path)
    intent_id = _insert_prepared_publish_foundation(store, job.job_id)
    with store.connect() as connection, governance_transaction(connection):
        connection.execute(
            """
            UPDATE governance_publish_intents
            SET status = 'published', resolved_at = ? WHERE intent_id = ?
            """,
            (NOW.isoformat(), intent_id),
        )
        connection.execute(
            """
            UPDATE governance_project_publish_gates
            SET active_intent_id = NULL, gate_revision = gate_revision + 1,
                state = 'unlocked', updated_at = ?
            WHERE project_namespace = ? AND project_id = ?
            """,
            (NOW.isoformat(), PROJECT.namespace, PROJECT.project_id),
        )

    with pytest.raises(GovernanceEventError, match="PUBLISH_INTENT_ROOT_MISMATCH"):
        store.check_startup()


def test_publish_prepare_atomically_roots_verified_candidate(tmp_path: Path) -> None:
    store, job = _publish_pending_job_fixture(tmp_path)
    git = FakePublishGit()
    service = PublishPreparationService(store, git, clock=lambda: NOW)

    prepared = service.prepare(
        job.job_id,
        fencing_token=job.fencing_token,
        canonical_ref="refs/heads/main",
        candidate_commit="b" * 40,
    )

    assert prepared.status.value == "prepared"
    assert prepared.expected_old_ref == "a13d92f" + "a" * 33
    assert prepared.candidate_commit == "b" * 40
    assert git.inspected == (
        b"immutable staged tree",
        b'{"canonical_ref":"refs/heads/main"}',
    )
    with store.connect() as connection:
        gate = connection.execute(
            """
            SELECT active_intent_id, gate_revision, state
            FROM governance_project_publish_gates
            """
        ).fetchone()
    assert gate == (prepared.intent_id, 1, "locked")
    assert store.check_startup().healthy


def test_publish_prepare_late_gate_failure_rolls_back_intent(tmp_path: Path) -> None:
    store, job = _publish_pending_job_fixture(tmp_path)
    with store.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER inject_publish_gate_failure
            BEFORE INSERT ON governance_project_publish_gates
            BEGIN SELECT RAISE(ABORT, 'injected publish gate failure'); END
            """
        )
    service = PublishPreparationService(store, FakePublishGit(), clock=lambda: NOW)

    with pytest.raises(sqlite3.IntegrityError, match="injected publish gate failure"):
        service.prepare(
            job.job_id,
            fencing_token=job.fencing_token,
            canonical_ref="refs/heads/main",
            candidate_commit="b" * 40,
        )
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM governance_publish_intents").fetchone() == (
            0,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_project_publish_gates"
        ).fetchone() == (0,)


def test_publish_prepare_rejects_stale_fence_and_base_revision(tmp_path: Path) -> None:
    store, job = _publish_pending_job_fixture(tmp_path)
    service = PublishPreparationService(store, FakePublishGit(), clock=lambda: NOW)
    with pytest.raises(PublishGovernanceError, match="PUBLISH_PREPARATION_ROOT_MISMATCH"):
        service.prepare(
            job.job_id,
            fencing_token=job.fencing_token + 1,
            canonical_ref="refs/heads/main",
            candidate_commit="b" * 40,
        )
    conflicting = PublishPreparationService(
        store,
        FakePublishGit(current_ref="d" * 40),
        clock=lambda: NOW,
    )
    with pytest.raises(PublishGovernanceError, match="PUBLISH_BASE_REVISION_CONFLICT"):
        conflicting.prepare(
            job.job_id,
            fencing_token=job.fencing_token,
            canonical_ref="refs/heads/main",
            candidate_commit="b" * 40,
        )


def test_publish_prepare_rejects_candidate_with_wrong_parent(tmp_path: Path) -> None:
    store, job = _publish_pending_job_fixture(tmp_path)
    service = PublishPreparationService(
        store,
        FakePublishGit(parent_commit="d" * 40),
        clock=lambda: NOW,
    )

    with pytest.raises(PublishGovernanceError, match="PUBLISH_CANDIDATE_BASE_MISMATCH"):
        service.prepare(
            job.job_id,
            fencing_token=job.fencing_token,
            canonical_ref="refs/heads/main",
            candidate_commit="b" * 40,
        )
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM governance_publish_intents").fetchone() == (
            0,
        )


@pytest.mark.parametrize("control", ("\x01", "\x7f"))
def test_publish_prepare_rejects_control_ref_before_git_call(
    tmp_path: Path,
    control: str,
) -> None:
    store, job = _publish_pending_job_fixture(tmp_path)
    git = FakePublishGit()
    service = PublishPreparationService(store, git, clock=lambda: NOW)

    with pytest.raises(ValueError, match="canonical ref"):
        service.prepare(
            job.job_id,
            fencing_token=job.fencing_token,
            canonical_ref=f"refs/heads/a/{control}b",
            candidate_commit="b" * 40,
        )
    assert git.inspected is None
    assert git.read_count == 0


def test_competing_publish_prepare_creates_one_locked_intent(tmp_path: Path) -> None:
    store, job = _publish_pending_job_fixture(tmp_path)
    barrier = threading.Barrier(2)

    def prepare() -> str:
        service = PublishPreparationService(store, FakePublishGit(), clock=lambda: NOW)
        barrier.wait()
        try:
            return service.prepare(
                job.job_id,
                fencing_token=job.fencing_token,
                canonical_ref="refs/heads/main",
                candidate_commit="b" * 40,
            ).status.value
        except PublishGovernanceError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(lambda _index: prepare(), range(2)))

    assert sorted(outcomes) == ["PUBLISH_GATE_LOCKED", "prepared"]
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM governance_publish_intents").fetchone() == (
            1,
        )
        assert connection.execute(
            """
            SELECT COUNT(*) FROM governance_project_publish_gates
            WHERE state = 'locked' AND active_intent_id IS NOT NULL
            """
        ).fetchone() == (1,)


def _real_prepared_publish_fixture(tmp_path: Path):
    repository = tmp_path / "canonical"
    repository.mkdir()

    def git(*arguments: str, text: bool = True):
        return subprocess.run(
            ("git", "-C", str(repository), *arguments),
            check=True,
            capture_output=True,
            text=text,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.name", "AMPLAI Test")
    git("config", "user.email", "test@example.invalid")
    tracked = repository / "artifact.txt"
    tracked.write_text("base\n", encoding="utf-8")
    git("add", "artifact.txt")
    git("commit", "-q", "-m", "base")
    base = git("rev-parse", "refs/heads/main").stdout.strip()
    tracked.write_text("candidate\n", encoding="utf-8")
    git("add", "artifact.txt")
    tree = git("write-tree").stdout.strip()
    candidate = git("commit-tree", tree, "-p", base, "-m", "candidate").stdout.strip()
    artifact = git(
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        f"{candidate}^{{tree}}",
        text=False,
    ).stdout
    request = json.dumps(
        {"candidate_commit": candidate, "canonical_ref": "refs/heads/main"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    store, job = _publish_pending_job_fixture(
        tmp_path,
        artifact_bytes=artifact,
        publish_request_bytes=request,
        base_revision=base[:7],
    )
    inspector = SubprocessGitCandidateInspector(repository)
    prepared = PublishPreparationService(store, inspector, clock=lambda: NOW).prepare(
        job.job_id,
        fencing_token=job.fencing_token,
        canonical_ref="refs/heads/main",
        candidate_commit=candidate,
    )
    return store, repository, git, prepared, base, candidate


def test_fenced_git_coordinator_requires_durable_prepared_intent(tmp_path: Path) -> None:
    store, repository, git, prepared, _base, candidate = _real_prepared_publish_fixture(tmp_path)
    coordinator = FencedGitPublishCoordinator(
        store,
        repository,
        coordinator_id="publish-coordinator-1",
        clock=lambda: NOW,
    )

    with pytest.raises(PublishGovernanceError, match="PUBLISH_INTENT_NOT_FOUND"):
        coordinator.publish_prepared_ref("PBI-FFFFFFFFFFFFFFFF")

    first = coordinator.publish_prepared_ref(prepared.intent_id)

    assert first.value == "updated"
    with (
        store.connect() as connection,
        pytest.raises(
            sqlite3.IntegrityError,
            match="active publish claim blocks intent transition",
        ),
    ):
        connection.execute(
            """
            UPDATE governance_publish_intents
            SET status = 'cancelled', resolved_at = ? WHERE intent_id = ?
            """,
            (NOW.isoformat(), prepared.intent_id),
        )
    with (
        store.connect() as connection,
        pytest.raises(
            sqlite3.IntegrityError,
            match="active publish claim blocks gate transition",
        ),
    ):
        connection.execute(
            """
            UPDATE governance_project_publish_gates
            SET state = 'unlocked', active_intent_id = NULL,
                gate_revision = gate_revision + 1, updated_at = ?
            WHERE active_intent_id = ?
            """,
            (NOW.isoformat(), prepared.intent_id),
        )
    with pytest.raises(PublishGovernanceError, match="PUBLISH_CLAIM_ACTIVE"):
        coordinator.publish_prepared_ref(prepared.intent_id)
    assert git("rev-parse", "refs/heads/main").stdout.strip() == candidate
    with store.connect() as connection:
        assert connection.execute(
            "SELECT status FROM governance_publish_intents WHERE intent_id = ?",
            (prepared.intent_id,),
        ).fetchone() == ("prepared",)
        assert connection.execute(
            """
            SELECT coordinator_id, claim_fencing_token, state
            FROM governance_publish_claims WHERE intent_id = ?
            """,
            (prepared.intent_id,),
        ).fetchone() == ("publish-coordinator-1", 1, "active")
    assert store.check_startup().healthy


def test_pre_cas_failure_releases_claim_and_retry_advances_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, repository, _git, prepared, _base, candidate = _real_prepared_publish_fixture(tmp_path)
    failing = FencedGitPublishCoordinator(
        store,
        repository,
        coordinator_id="publish-coordinator-failing",
        clock=lambda: NOW,
    )

    def reject_candidate(*_args, **_kwargs):
        raise PublishGovernanceError("PUBLISH_CANDIDATE_MISMATCH")

    monkeypatch.setattr(failing._backend, "inspect_candidate", reject_candidate)
    with pytest.raises(PublishGovernanceError, match="PUBLISH_CANDIDATE_MISMATCH"):
        failing.publish_prepared_ref(prepared.intent_id)

    retry = FencedGitPublishCoordinator(
        store,
        repository,
        coordinator_id="publish-coordinator-retry",
        clock=lambda: NOW,
    )
    assert retry.publish_prepared_ref(prepared.intent_id) is GitCASOutcome.UPDATED
    with store.connect() as connection:
        assert connection.execute(
            """
            SELECT coordinator_id, claim_fencing_token, state
            FROM governance_publish_claims WHERE intent_id = ?
            ORDER BY claim_fencing_token
            """,
            (prepared.intent_id,),
        ).fetchall() == [
            ("publish-coordinator-failing", 1, "released"),
            ("publish-coordinator-retry", 2, "active"),
        ]
    assert retry._backend.read_ref(prepared.canonical_ref) == candidate
    assert store.check_startup().healthy


def test_competing_publish_coordinators_create_one_active_claim(tmp_path: Path) -> None:
    store, repository, _git, prepared, _base, _candidate = _real_prepared_publish_fixture(tmp_path)
    barrier = threading.Barrier(2)

    def publish(index: int) -> str:
        coordinator = FencedGitPublishCoordinator(
            store,
            repository,
            coordinator_id=f"publish-coordinator-{index}",
            clock=lambda: NOW,
        )
        barrier.wait()
        try:
            return coordinator.publish_prepared_ref(prepared.intent_id).value
        except PublishGovernanceError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(publish, range(2)))

    assert sorted(outcomes) == ["PUBLISH_CLAIM_ACTIVE", "updated"]
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_publish_claims WHERE state = 'active'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_publish_claims WHERE intent_id = ?",
            (prepared.intent_id,),
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    "case",
    ("expected_unchanged", "conflict", "replay", "ambiguous"),
)
def test_post_cas_boundary_outcomes_retain_durable_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    store, repository, git, prepared, _base, candidate = _real_prepared_publish_fixture(tmp_path)
    coordinator = FencedGitPublishCoordinator(
        store,
        repository,
        coordinator_id=f"publish-coordinator-{case}",
        clock=lambda: NOW,
    )
    if case == "replay":
        git("update-ref", "refs/heads/main", candidate)
    elif case == "ambiguous":

        def ambiguous(*_args, **_kwargs):
            raise GitPublishAmbiguousError()

        monkeypatch.setattr(coordinator._backend, "_compare_and_swap_ref", ambiguous)
    else:
        outcome = (
            GitCASOutcome.EXPECTED_UNCHANGED
            if case == "expected_unchanged"
            else GitCASOutcome.CONFLICT
        )
        monkeypatch.setattr(
            coordinator._backend,
            "_compare_and_swap_ref",
            lambda *_args, **_kwargs: outcome,
        )

    if case == "ambiguous":
        with pytest.raises(GitPublishAmbiguousError):
            coordinator.publish_prepared_ref(prepared.intent_id)
    else:
        result = coordinator.publish_prepared_ref(prepared.intent_id)
        expected = GitCASOutcome.UPDATED if case == "replay" else outcome
        assert result is expected
    with store.connect() as connection:
        assert connection.execute(
            """
            SELECT claim_fencing_token, state FROM governance_publish_claims
            WHERE intent_id = ?
            """,
            (prepared.intent_id,),
        ).fetchone() == (1, "active")
    assert store.check_startup().healthy


def test_startup_rejects_publish_claim_fencing_gap(tmp_path: Path) -> None:
    store, job = _publish_pending_job_fixture(tmp_path)
    intent_id = _insert_prepared_publish_foundation(store, job.job_id)
    with store.connect() as connection:
        connection.execute(
            """
            INSERT INTO governance_publish_claims(
                claim_id, intent_id, project_namespace, project_id, coordinator_id,
                claim_fencing_token, state, claimed_at, resolved_at
            ) VALUES (?, ?, ?, ?, 'coordinator-gap', 2, 'released', ?, ?)
            """,
            (
                "PCL-0000000000000002",
                intent_id,
                PROJECT.namespace,
                PROJECT.project_id,
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )

    with pytest.raises(GovernanceEventError, match="PUBLISH_CLAIM_ROOT_MISMATCH"):
        store.initialize()


def test_startup_rejects_active_publish_claim_root_mismatch(tmp_path: Path) -> None:
    store, job = _publish_pending_job_fixture(tmp_path)
    intent_id = _insert_prepared_publish_foundation(store, job.job_id)
    connection = sqlite3.connect(store.path)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """
            INSERT INTO governance_publish_claims(
                claim_id, intent_id, project_namespace, project_id, coordinator_id,
                claim_fencing_token, state, claimed_at, resolved_at
            ) VALUES (?, ?, 'org/default/project/other', 'other',
                      'coordinator-root', 1, 'active', ?, NULL)
            """,
            ("PCL-0000000000000003", intent_id, NOW.isoformat()),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(GovernanceStoreError, match="foreign key integrity check"):
        store.initialize()


def test_post_cas_recovery_atomically_finalizes_publish(tmp_path: Path) -> None:
    store, repository, git, prepared, _base, candidate = _real_prepared_publish_fixture(tmp_path)
    coordinator = FencedGitPublishCoordinator(
        store,
        repository,
        coordinator_id="publisher-before-kill",
        clock=lambda: NOW,
    )
    assert coordinator.publish_prepared_ref(prepared.intent_id) is GitCASOutcome.UPDATED

    recovered = PublishResolutionService(
        store,
        SubprocessGitCandidateInspector(repository),
        coordinator_id="recovery-after-kill",
        clock=lambda: NOW,
    ).recover(prepared.intent_id)

    assert recovered.resolution_type is PublishResolutionType.PUBLISHED
    assert recovered.actual_ref == candidate
    assert recovered.applied_revision == candidate
    assert git("rev-parse", "refs/heads/main").stdout.strip() == candidate
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT state, resolved_at FROM governance_publish_claims WHERE intent_id = ?",
                (prepared.intent_id,),
            ).fetchone()[0]
            == "released"
        )
        assert connection.execute(
            "SELECT status FROM governance_publish_intents WHERE intent_id = ?",
            (prepared.intent_id,),
        ).fetchone() == ("published",)
        assert connection.execute(
            "SELECT outcome, actual_ref FROM governance_publish_results WHERE intent_id = ?",
            (prepared.intent_id,),
        ).fetchone() == ("published", candidate)
        assert connection.execute(
            "SELECT status FROM governance_apply_jobs WHERE job_id = ?",
            (prepared.job_id,),
        ).fetchone() == ("succeeded",)
        assert connection.execute(
            """
            SELECT status, applied_revision FROM governance_active_proposals
            WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
            """,
            (PROJECT.namespace, PROJECT.project_id, PROPOSAL.proposal_id),
        ).fetchone() == ("applied", candidate)
        assert connection.execute(
            "SELECT state, active_intent_id FROM governance_project_publish_gates"
        ).fetchone() == ("unlocked", None)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_audit_events WHERE event_type = 'publish.published'"
        ).fetchone() == (1,)
        assert connection.execute(
            """
            SELECT COUNT(*) FROM governance_outbox_events o
            JOIN governance_audit_events a ON a.aggregate_sequence = o.aggregate_sequence
             AND a.project_namespace = o.project_namespace AND a.project_id = o.project_id
             AND a.proposal_id = o.proposal_id
            WHERE a.event_type = 'publish.published'
            """
        ).fetchone() == (2,)
    assert store.check_startup().healthy


def test_pre_cas_recovery_releases_for_retry_without_state_loss(tmp_path: Path) -> None:
    store, repository, _git, prepared, _base, _candidate = _real_prepared_publish_fixture(tmp_path)

    recovered = PublishResolutionService(
        store,
        SubprocessGitCandidateInspector(repository),
        coordinator_id="pre-cas-recovery",
        clock=lambda: NOW,
    ).recover(prepared.intent_id)

    assert recovered.resolution_type is PublishResolutionType.RETRY_RELEASED
    assert recovered.intent_status == "prepared"
    assert recovered.job_status == "publish_pending"
    with store.connect() as connection:
        assert connection.execute(
            "SELECT state FROM governance_publish_claims WHERE intent_id = ?",
            (prepared.intent_id,),
        ).fetchone() == ("released",)
        assert connection.execute(
            "SELECT state, active_intent_id FROM governance_project_publish_gates"
        ).fetchone() == ("locked", prepared.intent_id)
        assert connection.execute("SELECT COUNT(*) FROM governance_publish_results").fetchone() == (
            0,
        )
    assert store.check_startup().healthy


def test_recovery_records_publish_conflict_and_job_hold(tmp_path: Path) -> None:
    store, repository, git, prepared, _base, candidate = _real_prepared_publish_fixture(tmp_path)
    tracked = repository / "other.txt"
    tracked.write_text("other\n", encoding="utf-8")
    git("add", "other.txt")
    other_tree = git("write-tree").stdout.strip()
    other = git("commit-tree", other_tree, "-p", candidate, "-m", "other").stdout.strip()
    git("update-ref", "refs/heads/main", other)

    recovered = PublishResolutionService(
        store,
        SubprocessGitCandidateInspector(repository),
        coordinator_id="conflict-recovery",
        clock=lambda: NOW,
    ).recover(prepared.intent_id)

    assert recovered.resolution_type is PublishResolutionType.PUBLISH_CONFLICT
    assert recovered.actual_ref == other
    with store.connect() as connection:
        assert connection.execute(
            "SELECT status FROM governance_apply_jobs WHERE job_id = ?",
            (prepared.job_id,),
        ).fetchone() == ("recovery_hold",)
        assert connection.execute(
            "SELECT status FROM governance_active_proposals WHERE proposal_id = ?",
            (PROPOSAL.proposal_id,),
        ).fetchone() == ("apply_requested",)
        assert connection.execute(
            "SELECT outcome, actual_ref FROM governance_publish_results WHERE intent_id = ?",
            (prepared.intent_id,),
        ).fetchone() == ("publish_conflict", other)


def test_ambiguous_ref_read_moves_intent_and_gate_to_recovery_hold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, repository, _git, prepared, _base, _candidate = _real_prepared_publish_fixture(tmp_path)
    inspector = SubprocessGitCandidateInspector(repository)

    def fail_read(_canonical_ref: str) -> str:
        raise PublishGovernanceError("PUBLISH_GIT_COMMAND_FAILED")

    monkeypatch.setattr(inspector, "read_ref", fail_read)
    recovered = PublishResolutionService(
        store,
        inspector,
        coordinator_id="ambiguous-recovery",
        clock=lambda: NOW,
    ).recover(prepared.intent_id)

    assert recovered.resolution_type is PublishResolutionType.RECOVERY_HOLD
    assert recovered.actual_ref is None
    with store.connect() as connection:
        assert connection.execute(
            "SELECT status, last_error_code FROM governance_publish_intents"
        ).fetchone() == ("recovery_hold", "PUBLISH_GIT_COMMAND_FAILED")
        assert connection.execute(
            "SELECT state FROM governance_project_publish_gates"
        ).fetchone() == ("recovery_hold",)
        assert connection.execute(
            "SELECT status FROM governance_active_proposals WHERE proposal_id = ?",
            (PROPOSAL.proposal_id,),
        ).fetchone() == ("apply_requested",)
    assert store.check_startup().healthy


def test_repeated_recovery_hold_refreshes_authoritative_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, repository, _git, prepared, _base, _candidate = _real_prepared_publish_fixture(tmp_path)
    inspector = SubprocessGitCandidateInspector(repository)
    errors = iter(("PUBLISH_GIT_COMMAND_FAILED", "PUBLISH_GIT_RESULT_AMBIGUOUS"))

    def fail_read(_canonical_ref: str) -> str:
        raise PublishGovernanceError(next(errors))

    monkeypatch.setattr(inspector, "read_ref", fail_read)
    service = PublishResolutionService(
        store, inspector, coordinator_id="repeat-hold", clock=lambda: NOW
    )
    service.recover(prepared.intent_id)
    service.recover(prepared.intent_id)

    with store.connect() as connection:
        assert connection.execute(
            "SELECT status, last_error_code FROM governance_publish_intents"
        ).fetchone() == ("recovery_hold", "PUBLISH_GIT_RESULT_AMBIGUOUS")
        assert connection.execute(
            "SELECT status, last_error_code FROM governance_apply_jobs"
        ).fetchone() == ("recovery_hold", "PUBLISH_GIT_RESULT_AMBIGUOUS")
        assert connection.execute(
            """
            SELECT before_job_status, stream_revision
            FROM governance_publish_resolution_roots ORDER BY stream_revision
            """
        ).fetchall() == [("publish_pending", 4), ("recovery_hold", 5)]
    assert store.check_startup().healthy


def test_late_publish_outbox_failure_rolls_back_database_finalization(
    tmp_path: Path,
) -> None:
    store, repository, _git, prepared, _base, candidate = _real_prepared_publish_fixture(tmp_path)
    FencedGitPublishCoordinator(
        store,
        repository,
        coordinator_id="publisher-before-outbox-failure",
        clock=lambda: NOW,
    ).publish_prepared_ref(prepared.intent_id)
    with store.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER inject_publish_outbox_failure
            BEFORE INSERT ON governance_outbox_events
            WHEN NEW.destination_ref LIKE 'apply-job:%:observer'
            BEGIN SELECT RAISE(ABORT, 'injected publish outbox failure'); END
            """
        )
    service = PublishResolutionService(
        store,
        SubprocessGitCandidateInspector(repository),
        coordinator_id="publish-finalizer",
        clock=lambda: NOW,
    )

    with pytest.raises(sqlite3.IntegrityError, match="injected publish outbox failure"):
        service.recover(prepared.intent_id)

    with store.connect() as connection:
        assert connection.execute(
            "SELECT state FROM governance_publish_claims WHERE intent_id = ?",
            (prepared.intent_id,),
        ).fetchone() == ("active",)
        assert connection.execute(
            "SELECT status FROM governance_publish_intents WHERE intent_id = ?",
            (prepared.intent_id,),
        ).fetchone() == ("prepared",)
        assert connection.execute(
            "SELECT status FROM governance_apply_jobs WHERE job_id = ?",
            (prepared.job_id,),
        ).fetchone() == ("publish_pending",)
        assert connection.execute("SELECT COUNT(*) FROM governance_publish_results").fetchone() == (
            0,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_publish_resolution_events"
        ).fetchone() == (0,)
        connection.execute("DROP TRIGGER inject_publish_outbox_failure")

    recovered = service.recover(prepared.intent_id)
    assert recovered.resolution_type is PublishResolutionType.PUBLISHED
    assert recovered.applied_revision == candidate


@pytest.mark.parametrize(
    ("method", "expected_intent", "expected_proposal"),
    (
        ("fail", "failed", "apply_failed"),
        ("cancel", "cancelled", "apply_failed"),
    ),
)
def test_unchanged_ref_can_be_failed_or_cancelled_authoritatively(
    tmp_path: Path,
    method: str,
    expected_intent: str,
    expected_proposal: str,
) -> None:
    store, repository, _git, prepared, base, _candidate = _real_prepared_publish_fixture(tmp_path)
    service = PublishResolutionService(
        store,
        SubprocessGitCandidateInspector(repository),
        coordinator_id=f"{method}-resolver",
        clock=lambda: NOW,
    )

    resolved = (
        service.fail_if_unchanged(prepared.intent_id, error_code="PUBLISH_POLICY_FAILED")
        if method == "fail"
        else service.cancel_if_unchanged(prepared.intent_id)
    )

    assert resolved.intent_status == expected_intent
    assert resolved.proposal_status == expected_proposal
    assert resolved.actual_ref == base
    with store.connect() as connection:
        assert connection.execute(
            "SELECT status FROM governance_apply_jobs WHERE job_id = ?",
            (prepared.job_id,),
        ).fetchone() == ("dead_letter",)
        assert connection.execute(
            "SELECT status, applied_revision FROM governance_active_proposals"
        ).fetchone() == (expected_proposal, None)
        assert connection.execute(
            "SELECT state FROM governance_project_publish_gates"
        ).fetchone() == ("unlocked",)
    assert store.check_startup().healthy


@pytest.mark.parametrize("method", ("fail", "cancel"))
def test_fail_or_cancel_converges_candidate_to_published(
    tmp_path: Path,
    method: str,
) -> None:
    store, repository, git, prepared, base, candidate = _real_prepared_publish_fixture(tmp_path)
    git("update-ref", prepared.canonical_ref, candidate, base)
    service = PublishResolutionService(
        store,
        SubprocessGitCandidateInspector(repository),
        coordinator_id=f"{method}-candidate-resolver",
        clock=lambda: NOW,
    )

    resolved = (
        service.fail_if_unchanged(prepared.intent_id, error_code="PUBLISH_POLICY_FAILED")
        if method == "fail"
        else service.cancel_if_unchanged(prepared.intent_id)
    )

    assert resolved.resolution_type is PublishResolutionType.PUBLISHED
    assert resolved.applied_revision == candidate
    assert store.check_startup().healthy


def test_public_publish_workflow_finalizes_and_terminal_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    store, repository, _git, prepared, _base, candidate = _real_prepared_publish_fixture(tmp_path)
    workflow = FencedGitPublishWorkflow(
        FencedGitPublishCoordinator(
            store, repository, coordinator_id="workflow-publisher", clock=lambda: NOW
        ),
        PublishResolutionService(
            store,
            SubprocessGitCandidateInspector(repository),
            coordinator_id="workflow-resolver",
            clock=lambda: NOW,
        ),
    )

    first = workflow.publish(prepared.intent_id)
    replay = workflow.publish(prepared.intent_id)

    assert first.resolution_type is PublishResolutionType.PUBLISHED
    assert first.applied_revision == candidate
    assert replay.resolution_event_id == first.resolution_event_id
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_publish_resolution_events"
        ).fetchone() == (1,)
    assert store.check_startup().healthy


def test_public_publish_workflow_routes_operational_git_failure_to_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, repository, _git, prepared, _base, _candidate = _real_prepared_publish_fixture(tmp_path)
    coordinator = FencedGitPublishCoordinator(
        store, repository, coordinator_id="failing-workflow-publisher", clock=lambda: NOW
    )

    def fail_ref(_canonical_ref: str) -> str:
        raise PublishGovernanceError("PUBLISH_GIT_COMMAND_FAILED")

    monkeypatch.setattr(coordinator._backend, "read_ref", fail_ref)
    workflow = FencedGitPublishWorkflow(
        coordinator,
        PublishResolutionService(
            store,
            SubprocessGitCandidateInspector(repository),
            coordinator_id="workflow-recovery",
            clock=lambda: NOW,
        ),
    )

    resolved = workflow.publish(prepared.intent_id)
    assert resolved.resolution_type is PublishResolutionType.RETRY_RELEASED
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_publish_resolution_events"
        ).fetchone() == (1,)
    assert store.check_startup().healthy


def test_live_publisher_and_cancel_serialize_before_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, repository, git, prepared, _base, candidate = _real_prepared_publish_fixture(tmp_path)
    coordinator = FencedGitPublishCoordinator(
        store, repository, coordinator_id="live-publisher", clock=lambda: NOW
    )
    entered_cas = threading.Event()
    release_cas = threading.Event()
    original_cas = coordinator._backend._compare_and_swap_ref

    def blocked_cas(*args, **kwargs):
        entered_cas.set()
        assert release_cas.wait(timeout=5)
        return original_cas(*args, **kwargs)

    monkeypatch.setattr(coordinator._backend, "_compare_and_swap_ref", blocked_cas)
    with ThreadPoolExecutor(max_workers=2) as executor:
        publishing = executor.submit(coordinator.publish_prepared_ref, prepared.intent_id)
        assert entered_cas.wait(timeout=5)
        cancelling = executor.submit(
            PublishResolutionService(
                store,
                SubprocessGitCandidateInspector(repository),
                coordinator_id="cancel-resolver",
                clock=lambda: NOW,
            ).cancel_if_unchanged,
            prepared.intent_id,
        )
        release_cas.set()
        assert publishing.result(timeout=5) is GitCASOutcome.UPDATED
        resolved = cancelling.result(timeout=5)

    assert resolved.resolution_type is PublishResolutionType.PUBLISHED
    assert git("rev-parse", prepared.canonical_ref).stdout.strip() == candidate
    assert store.check_startup().healthy


def test_cancel_observation_blocks_late_publisher_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, repository, git, prepared, base, _candidate = _real_prepared_publish_fixture(tmp_path)
    inspector = SubprocessGitCandidateInspector(repository)
    observation_started = threading.Event()
    release_observation = threading.Event()
    original_read = inspector.read_ref

    def blocked_read(canonical_ref: str) -> str:
        observation_started.set()
        assert release_observation.wait(timeout=5)
        return original_read(canonical_ref)

    monkeypatch.setattr(inspector, "read_ref", blocked_read)
    resolver = PublishResolutionService(
        store, inspector, coordinator_id="cancel-first", clock=lambda: NOW
    )
    publisher = FencedGitPublishCoordinator(
        store, repository, coordinator_id="publisher-second", clock=lambda: NOW
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        cancelling = executor.submit(resolver.cancel_if_unchanged, prepared.intent_id)
        assert observation_started.wait(timeout=5)
        publishing = executor.submit(publisher.publish_prepared_ref, prepared.intent_id)
        release_observation.set()
        resolved = cancelling.result(timeout=5)
        with pytest.raises(PublishGovernanceError, match="PUBLISH_INTENT_NOT_PREPARED"):
            publishing.result(timeout=5)

    assert resolved.resolution_type is PublishResolutionType.CANCELLED
    assert git("rev-parse", prepared.canonical_ref).stdout.strip() == base
    assert store.check_startup().healthy


def test_publish_resolution_commit_ambiguity_replays_terminal_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, repository, _git, prepared, _base, _candidate = _real_prepared_publish_fixture(tmp_path)
    FencedGitPublishCoordinator(
        store, repository, coordinator_id="ambiguity-publisher", clock=lambda: NOW
    ).publish_prepared_ref(prepared.intent_id)
    service = PublishResolutionService(
        store,
        SubprocessGitCandidateInspector(repository),
        coordinator_id="ambiguity-resolver",
        clock=lambda: NOW,
    )
    original_connect = store.connect
    commits = 0

    class CommitAmbiguousAfterSuccess:
        def __init__(self, connection):
            self.connection = connection

        @property
        def in_transaction(self):
            return self.connection.in_transaction

        def execute(self, statement, parameters=()):
            nonlocal commits
            result = self.connection.execute(statement, parameters)
            if statement == "COMMIT":
                commits += 1
                if commits == 2:
                    raise sqlite3.OperationalError("response lost after commit")
            return result

        def __getattr__(self, name):
            return getattr(self.connection, name)

    @contextmanager
    def ambiguous_connect():
        with original_connect() as connection:
            yield CommitAmbiguousAfterSuccess(connection)

    monkeypatch.setattr(store, "connect", ambiguous_connect)
    with pytest.raises(GovernanceCommitAmbiguousError):
        service.recover(prepared.intent_id)
    monkeypatch.setattr(store, "connect", original_connect)

    replay = service.recover(prepared.intent_id)
    assert replay.resolution_type is PublishResolutionType.PUBLISHED
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM governance_publish_results").fetchone() == (
            1,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_publish_resolution_events"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_audit_events WHERE event_type = 'publish.published'"
        ).fetchone() == (1,)
    assert store.check_startup().healthy


def test_process_kill_before_cas_preserves_retryable_database_truth(tmp_path: Path) -> None:
    store, repository, git, prepared, base, _candidate = _real_prepared_publish_fixture(tmp_path)
    marker = tmp_path / "before-cas.marker"
    process = multiprocessing.get_context("spawn").Process(
        target=_hard_kill_before_cas,
        args=(str(store.path), str(repository), prepared.intent_id, str(marker)),
    )
    process.start()
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.exists()
    process.terminate()
    process.join(timeout=5)

    assert git("rev-parse", prepared.canonical_ref).stdout.strip() == base
    recovered = PublishResolutionService(
        store,
        SubprocessGitCandidateInspector(repository),
        coordinator_id="recover-pre-cas-kill",
        clock=lambda: NOW,
    ).recover(prepared.intent_id)
    assert recovered.resolution_type is PublishResolutionType.RETRY_RELEASED
    assert store.check_startup().healthy


def test_process_kill_after_cas_recovers_exactly_once(tmp_path: Path) -> None:
    store, repository, git, prepared, _base, candidate = _real_prepared_publish_fixture(tmp_path)
    marker = tmp_path / "after-cas.marker"
    process = multiprocessing.get_context("spawn").Process(
        target=_hard_kill_after_cas,
        args=(str(store.path), str(repository), prepared.intent_id, str(marker)),
    )
    process.start()
    process.join(timeout=10)
    assert not process.is_alive()
    assert marker.exists()
    assert git("rev-parse", prepared.canonical_ref).stdout.strip() == candidate

    service = PublishResolutionService(
        store,
        SubprocessGitCandidateInspector(repository),
        coordinator_id="recover-post-cas-kill",
        clock=lambda: NOW,
    )
    first = service.recover(prepared.intent_id)
    replay = service.recover(prepared.intent_id)
    assert first.resolution_type is PublishResolutionType.PUBLISHED
    assert replay.resolution_event_id == first.resolution_event_id
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM governance_publish_results").fetchone() == (
            1,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_publish_resolution_events"
        ).fetchone() == (1,)
    assert store.check_startup().healthy


@pytest.mark.parametrize("observed", ("expected", "candidate"))
def test_recovery_hold_can_resume_from_observed_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observed: str,
) -> None:
    store, repository, git, prepared, base, candidate = _real_prepared_publish_fixture(tmp_path)
    failing_inspector = SubprocessGitCandidateInspector(repository)

    def fail_read(_canonical_ref: str) -> str:
        raise PublishGovernanceError("PUBLISH_GIT_COMMAND_FAILED")

    monkeypatch.setattr(failing_inspector, "read_ref", fail_read)
    held = PublishResolutionService(
        store,
        failing_inspector,
        coordinator_id="hold-resolver",
        clock=lambda: NOW,
    ).recover(prepared.intent_id)
    assert held.resolution_type is PublishResolutionType.RECOVERY_HOLD
    if observed == "candidate":
        git("update-ref", "refs/heads/main", candidate, base)

    resumed = PublishResolutionService(
        store,
        SubprocessGitCandidateInspector(repository),
        coordinator_id="resume-resolver",
        clock=lambda: NOW,
    ).recover(prepared.intent_id)

    expected = (
        PublishResolutionType.PUBLISHED
        if observed == "candidate"
        else PublishResolutionType.RETRY_RELEASED
    )
    assert resumed.resolution_type is expected
    assert resumed.resolution_sequence == 2
    with store.connect() as connection:
        assert connection.execute(
            """
            SELECT claim_fencing_token, state FROM governance_publish_claims
            WHERE intent_id = ? ORDER BY claim_fencing_token
            """,
            (prepared.intent_id,),
        ).fetchall() == [(1, "released"), (2, "released")]
    assert store.check_startup().healthy


def test_competing_recovery_finalizers_create_one_terminal_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, repository, _git, prepared, _base, candidate = _real_prepared_publish_fixture(tmp_path)
    FencedGitPublishCoordinator(
        store,
        repository,
        coordinator_id="publisher",
        clock=lambda: NOW,
    ).publish_prepared_ref(prepared.intent_id)
    barrier = threading.Barrier(2)
    original_read = SubprocessGitCandidateInspector(repository).read_ref

    def resolve(index: int) -> str:
        inspector = SubprocessGitCandidateInspector(repository)

        def synchronized_read(canonical_ref: str) -> str:
            value = original_read(canonical_ref)
            barrier.wait()
            return value

        monkeypatch.setattr(inspector, "read_ref", synchronized_read)
        try:
            return (
                PublishResolutionService(
                    store,
                    inspector,
                    coordinator_id=f"recovery-{index}",
                    clock=lambda: NOW,
                )
                .recover(prepared.intent_id)
                .resolution_type.value
            )
        except PublishGovernanceError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(resolve, range(2)))

    assert sorted(outcomes) == ["PUBLISH_CLAIM_STALE", "published"]
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_publish_results WHERE actual_ref = ?",
            (candidate,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_publish_resolution_events"
        ).fetchone() == (1,)


def test_startup_rejects_publish_resolution_state_tamper(tmp_path: Path) -> None:
    store, repository, _git, prepared, _base, _candidate = _real_prepared_publish_fixture(tmp_path)
    FencedGitPublishCoordinator(
        store,
        repository,
        coordinator_id="publisher",
        clock=lambda: NOW,
    ).publish_prepared_ref(prepared.intent_id)
    PublishResolutionService(
        store,
        SubprocessGitCandidateInspector(repository),
        coordinator_id="resolver",
        clock=lambda: NOW,
    ).recover(prepared.intent_id)
    with store.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE governance_publish_resolution_events SET error_code = 'tampered'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="durable"):
            connection.execute("DELETE FROM governance_publish_resolution_events")
        connection.execute(
            "UPDATE governance_active_proposals SET applied_revision = ?",
            ("f" * 40,),
        )

    with pytest.raises(GovernanceEventError, match="PUBLISH_RESOLUTION_ROOT_MISMATCH"):
        store.initialize()


def test_startup_rejects_unrooted_applied_revision(tmp_path: Path) -> None:
    store, _repository, _git, _prepared, _base, _candidate = _real_prepared_publish_fixture(
        tmp_path
    )
    with store.connect() as connection:
        connection.execute(
            """
            UPDATE governance_active_proposals
            SET status = 'applied', applied_revision = ?, state_revision = state_revision + 1
            """,
            ("f" * 40,),
        )

    with pytest.raises(GovernanceEventError, match="PUBLISH_RESOLUTION_ROOT_MISMATCH"):
        store.initialize()
