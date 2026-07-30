from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
    DecisionService,
    DirectAuthorityRequest,
    ImmutableDefinitionObjectStore,
    ProposalDefinitionManifest,
    ProposalRef,
    ProposalSubmissionService,
    canonicalize_definition,
)
from amplai_foundry.governance.apply_jobs import ApplyGovernanceError, ApplyGrantService
from amplai_foundry.governance.store import GovernanceStore

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


def _fixture(tmp_path: Path, *, grant_apply_permission: bool = True):
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
    return (
        store,
        active,
        ApplyGrantService(store, authority, objects, clock=lambda: NOW),
        decision_key,
    )


def test_approved_decision_issues_snapshot_scoped_hash_only_grant(tmp_path: Path) -> None:
    store, _active, grants, decision_key = _fixture(tmp_path)

    issued = grants._issue_from_approved_decision(
        decision_key,
        authority_request=_request(),
    )

    assert issued.record.proposal_ref == PROPOSAL
    assert issued.record.allowed_actor_ref == ACTOR
    assert issued.record.bound_channel_ref == CHANNEL
    assert "raw_grant=<redacted>" in repr(issued)
    snapshot = grants.get_snapshot(issued.record.snapshot_id)
    assert snapshot.expected_base_revision == "a13d92f"
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
