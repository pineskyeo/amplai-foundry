from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance import (
    ActorRef,
    ActorType,
    AuthorityContext,
    AuthorityPermission,
    AuthoritySource,
    ChannelProvider,
    ChannelRef,
    FileProposalActionLedger,
    ProposalAction,
    ProposalActionError,
    ProposalActionService,
    ProposalActionType,
    ProposalRef,
    proposal_digest,
)
from amplai_foundry.proposals.models import (
    Confidence,
    EvidenceLocator,
    OperationType,
    Proposal,
    ProposalEvidence,
    ProposalOperation,
    ProposalStatus,
)
from amplai_foundry.proposals.repository import ProposalRepository

NOW = datetime(2026, 7, 29, 17, 0, tzinfo=ZoneInfo("Asia/Seoul"))
PROJECT = ProjectRef(project_id="amplai", namespace="org/default/project/amplai")
ACTOR = ActorRef(actor_id="ACT-USER-1", actor_type=ActorType.HUMAN)
CHANNEL = ChannelRef(
    provider=ChannelProvider.SLACK,
    workspace_id="T123",
    channel_id="C456",
    message_id="1710000000.000200",
)


class FixedTokenVerifier:
    def __init__(self, valid_token: str = "token-valid-123") -> None:
        self.valid_token = valid_token
        self.calls = 0

    def verify(self, action: ProposalAction, authority: AuthorityContext) -> None:
        self.calls += 1
        if action.action_token != self.valid_token:
            raise ProposalActionError("ACTION_TOKEN_INVALID", "Action token이 유효하지 않습니다.")
        assert authority.actor_ref == action.actor_ref


def proposal(*, status: ProposalStatus = ProposalStatus.REVIEWED) -> Proposal:
    return Proposal(
        proposal_id="PROP-20260729-A1B2C3D4",
        project="amplai",
        namespace=PROJECT.namespace,
        status=status,
        source_ids=["SRC-20260729-A1B2C3D4"],
        created_at=NOW,
        created_by="codex",
        approved_at=NOW if status in {ProposalStatus.APPROVED, ProposalStatus.APPLIED} else None,
        approved_by=(
            ACTOR.actor_id if status in {ProposalStatus.APPROVED, ProposalStatus.APPLIED} else None
        ),
        operations=[
            ProposalOperation(
                operation_id="OP-001",
                type=OperationType.IGNORE,
                kind="concept",
                title="No-write fixture",
                reason="Proposal action contract를 검증한다.",
                evidence=[
                    ProposalEvidence(
                        source_id="SRC-20260729-A1B2C3D4",
                        locator=EvidenceLocator(start=1, end=1),
                    )
                ],
                confidence=Confidence.HIGH,
            )
        ],
    )


def authority(
    *,
    permissions: frozenset[AuthorityPermission] | None = None,
    channel: ChannelRef = CHANNEL,
) -> AuthorityContext:
    return AuthorityContext(
        actor_ref=ACTOR,
        project_ref=PROJECT,
        permissions=permissions or frozenset({AuthorityPermission.PROPOSAL_DECIDE}),
        source=AuthoritySource(request_id="REQ-001", channel=channel),
        authenticated_at=NOW,
    )


def action(
    current: Proposal,
    *,
    action_type: ProposalActionType = ProposalActionType.APPROVE,
    idempotency_key: str = "slack:T123:C456:message:approve",
    actor: ActorRef = ACTOR,
    channel: ChannelRef = CHANNEL,
    token: str = "token-valid-123",
) -> ProposalAction:
    return ProposalAction(
        proposal_ref=ProposalRef(project_ref=PROJECT, proposal_id=current.proposal_id),
        expected_version=current.revision,
        expected_digest=proposal_digest(current),
        action=action_type,
        actor_ref=actor,
        channel_ref=channel,
        action_token=token,
        idempotency_key=idempotency_key,
        occurred_at=NOW,
    )


def service(
    tmp_path: Path,
    current: Proposal,
    verifier: FixedTokenVerifier | None = None,
) -> tuple[ProposalActionService, ProposalRepository, FileProposalActionLedger, FixedTokenVerifier]:
    repository = ProposalRepository(tmp_path / ".amplai/proposals")
    repository.save(current)
    ledger = FileProposalActionLedger(tmp_path / ".amplai/audit/proposal-actions")
    selected_verifier = verifier or FixedTokenVerifier()
    action_service = ProposalActionService(
        repository,
        ledger,
        selected_verifier,
        lock_path=tmp_path / ".amplai/audit/proposal-actions.lock",
    )
    return action_service, repository, ledger, selected_verifier


def test_approve_revalidates_authority_and_records_idempotent_audit(tmp_path: Path) -> None:
    current = proposal()
    action_service, repository, ledger, verifier = service(tmp_path, current)
    request = action(current)

    first = action_service.execute(request, authority(), now=NOW)
    replay = action_service.execute(request, authority(), now=NOW)

    with pytest.raises(ProposalActionError, match="AUTHORITY_DENIED"):
        action_service.execute(
            request,
            authority(permissions=frozenset({AuthorityPermission.PROPOSAL_READ})),
            now=NOW,
        )

    stored = repository.get(current.proposal_id)
    assert stored is not None
    assert stored.status is ProposalStatus.APPROVED
    assert stored.revision == 2
    assert stored.approved_by == ACTOR.actor_id
    assert first == replay
    assert first.proposal_digest == proposal_digest(stored)
    assert ledger.get(request.idempotency_key).audit.before_status is ProposalStatus.REVIEWED  # type: ignore[union-attr]
    assert verifier.calls == 1


@pytest.mark.parametrize(
    ("action_type", "expected_status"),
    [
        (ProposalActionType.REJECT, ProposalStatus.REJECTED),
        (ProposalActionType.REQUEST_CHANGES, ProposalStatus.CHANGES_REQUESTED),
    ],
)
def test_reject_and_request_changes_use_explicit_transitions(
    tmp_path: Path,
    action_type: ProposalActionType,
    expected_status: ProposalStatus,
) -> None:
    current = proposal()
    action_service, repository, _ledger, _verifier = service(tmp_path, current)

    result = action_service.execute(action(current, action_type=action_type), authority(), now=NOW)

    assert result.proposal_status is expected_status
    assert repository.get(current.proposal_id).status is expected_status  # type: ignore[union-attr]


def test_stale_version_or_digest_cannot_change_proposal(tmp_path: Path) -> None:
    current = proposal()
    action_service, repository, _ledger, _verifier = service(tmp_path, current)
    stale = action(current).model_copy(update={"expected_version": 9})

    with pytest.raises(ProposalActionError, match="PROPOSAL_STALE"):
        action_service.execute(stale, authority(), now=NOW)

    assert repository.get(current.proposal_id) == current


def test_actor_channel_and_permission_must_match_server_authority(tmp_path: Path) -> None:
    current = proposal()
    action_service, repository, _ledger, _verifier = service(tmp_path, current)
    other_actor = ActorRef(actor_id="ACT-USER-2", actor_type=ActorType.HUMAN)
    other_channel = CHANNEL.model_copy(update={"message_id": "different-message"})

    with pytest.raises(ProposalActionError, match="ACTION_ACTOR_MISMATCH"):
        action_service.execute(action(current, actor=other_actor), authority(), now=NOW)
    with pytest.raises(ProposalActionError, match="ACTION_CHANNEL_MISMATCH"):
        action_service.execute(action(current, channel=other_channel), authority(), now=NOW)
    with pytest.raises(ProposalActionError, match="AUTHORITY_DENIED"):
        action_service.execute(
            action(current),
            authority(permissions=frozenset({AuthorityPermission.PROPOSAL_READ})),
            now=NOW,
        )

    assert repository.get(current.proposal_id) == current


def test_invalid_token_and_idempotency_conflict_fail_closed(tmp_path: Path) -> None:
    current = proposal()
    action_service, repository, _ledger, _verifier = service(tmp_path, current)

    with pytest.raises(ProposalActionError, match="ACTION_TOKEN_INVALID"):
        action_service.execute(action(current, token="token-invalid-1"), authority(), now=NOW)

    accepted = action(current)
    action_service.execute(accepted, authority(), now=NOW)
    conflicting = accepted.model_copy(update={"action_token": "token-valid-456"})
    with pytest.raises(ProposalActionError, match="IDEMPOTENCY_CONFLICT"):
        action_service.execute(conflicting, authority(), now=NOW)

    assert repository.get(current.proposal_id).status is ProposalStatus.APPROVED  # type: ignore[union-attr]


def test_apply_action_is_separate_and_deferred_to_worker_gate(tmp_path: Path) -> None:
    approved = proposal(status=ProposalStatus.APPROVED)
    action_service, repository, _ledger, _verifier = service(tmp_path, approved)
    request = action(approved, action_type=ProposalActionType.APPLY)

    with pytest.raises(ProposalActionError, match="APPLY_ACTION_DEFERRED"):
        action_service.execute(
            request,
            authority(permissions=frozenset({AuthorityPermission.PROPOSAL_APPLY})),
            now=NOW,
        )

    assert repository.get(approved.proposal_id) == approved
