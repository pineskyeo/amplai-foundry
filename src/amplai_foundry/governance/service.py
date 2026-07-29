"""Fail-closed Proposal decision actions independent of messenger providers."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from amplai_foundry.governance.ledger import (
    ProposalActionLedger,
    ProposalActionLedgerError,
)
from amplai_foundry.governance.models import (
    AuthorityContext,
    AuthorityPermission,
    ProposalAction,
    ProposalActionAuditEvent,
    ProposalActionResult,
    ProposalActionType,
    StoredProposalAction,
)
from amplai_foundry.proposals.apply import approve_proposal
from amplai_foundry.proposals.models import Proposal, ProposalStatus
from amplai_foundry.proposals.repository import ProposalRepository


class ProposalActionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code} {message}")


class ActionTokenVerifier(Protocol):
    """Port implemented by the secure one-time token package."""

    def verify(self, action: ProposalAction, authority: AuthorityContext) -> None: ...


def proposal_digest(proposal: Proposal) -> str:
    payload = json.dumps(
        proposal.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class ProposalActionService:
    """Validate authority and commit one idempotent Proposal decision action."""

    def __init__(
        self,
        proposal_repository: ProposalRepository,
        ledger: ProposalActionLedger,
        token_verifier: ActionTokenVerifier,
        *,
        lock_path: Path = Path(".amplai/audit/proposal-actions.lock"),
    ) -> None:
        self.proposal_repository = proposal_repository
        self.ledger = ledger
        self.token_verifier = token_verifier
        self.lock_path = lock_path

    def execute(
        self,
        action: ProposalAction,
        authority: AuthorityContext,
        *,
        now: datetime | None = None,
    ) -> ProposalActionResult:
        self._validate_request_authority(action, authority)
        request_fingerprint = self._request_fingerprint(action)
        replay = self._idempotent_result(action.idempotency_key, request_fingerprint)
        if replay is not None:
            return replay

        with self._lock():
            replay = self._idempotent_result(action.idempotency_key, request_fingerprint)
            if replay is not None:
                return replay
            proposal = self.proposal_repository.get(action.proposal_ref.proposal_id)
            if proposal is None:
                raise ProposalActionError("PROPOSAL_NOT_FOUND", "Proposal을 찾을 수 없습니다.")
            self._validate_proposal_identity(action, proposal)
            self.token_verifier.verify(action, authority)
            current_digest = proposal_digest(proposal)
            if action.expected_version != proposal.revision or (
                action.expected_digest != current_digest
            ):
                raise ProposalActionError(
                    "PROPOSAL_STALE",
                    f"current_version={proposal.revision} current_digest={current_digest}",
                )
            processed_at = now or datetime.now(ZoneInfo("Asia/Seoul"))
            updated = self._transition(proposal, action, processed_at)
            self.proposal_repository.save(updated)
            result, audit = self._result_and_audit(
                proposal,
                updated,
                action,
                authority,
                processed_at,
                request_fingerprint,
            )
            try:
                self.ledger.record(
                    StoredProposalAction(
                        request_fingerprint=request_fingerprint,
                        result=result,
                        audit=audit,
                    )
                )
            except ProposalActionLedgerError:
                self.proposal_repository.save(proposal)
                raise
            return result

    @staticmethod
    def _validate_request_authority(
        action: ProposalAction,
        authority: AuthorityContext,
    ) -> None:
        project_ref = action.proposal_ref.project_ref
        if authority.project_ref != project_ref:
            raise ProposalActionError("AUTHORITY_DENIED", "Authority project가 다릅니다.")
        if action.actor_ref != authority.actor_ref:
            raise ProposalActionError("ACTION_ACTOR_MISMATCH", "Action actor가 다릅니다.")
        if action.channel_ref != authority.source.channel:
            raise ProposalActionError("ACTION_CHANNEL_MISMATCH", "Action channel이 다릅니다.")
        ProposalActionService._validate_authority(action, authority)

    @staticmethod
    def _validate_proposal_identity(
        action: ProposalAction,
        proposal: Proposal,
    ) -> None:
        project_ref = action.proposal_ref.project_ref
        if (
            proposal.project != project_ref.project_id
            or proposal.namespace != project_ref.namespace
        ):
            raise ProposalActionError("PROPOSAL_NOT_FOUND", "Qualified ProposalRef가 다릅니다.")

    @staticmethod
    def _validate_authority(action: ProposalAction, authority: AuthorityContext) -> None:
        permission = (
            AuthorityPermission.PROPOSAL_APPLY
            if action.action is ProposalActionType.APPLY
            else AuthorityPermission.PROPOSAL_DECIDE
        )
        if permission not in authority.permissions:
            raise ProposalActionError("AUTHORITY_DENIED", f"{permission.value} 권한이 없습니다.")

    @staticmethod
    def _transition(
        proposal: Proposal,
        action: ProposalAction,
        processed_at: datetime,
    ) -> Proposal:
        decision_states = {ProposalStatus.DRAFT, ProposalStatus.REVIEWED}
        if action.action is ProposalActionType.APPLY:
            if proposal.status is not ProposalStatus.APPROVED:
                raise ProposalActionError(
                    "APPLY_REQUIRES_APPROVAL", "approved Proposal만 apply를 요청할 수 있습니다."
                )
            raise ProposalActionError(
                "APPLY_ACTION_DEFERRED",
                "Apply 실행은 Action Token과 Worker gate가 준비된 별도 service가 소유합니다.",
            )
        if proposal.status not in decision_states:
            raise ProposalActionError(
                "INVALID_PROPOSAL_STATE",
                f"status={proposal.status.value} Proposal에는 decision action을 "
                "수행할 수 없습니다.",
            )
        if action.action is ProposalActionType.APPROVE:
            approved = approve_proposal(
                proposal,
                approved_by=action.actor_ref.actor_id,
                now=processed_at,
            )
            return approved.model_copy(update={"revision": proposal.revision + 1})
        status = (
            ProposalStatus.REJECTED
            if action.action is ProposalActionType.REJECT
            else ProposalStatus.CHANGES_REQUESTED
        )
        return proposal.model_copy(
            update={"status": status, "revision": proposal.revision + 1},
        )

    @staticmethod
    def _result_and_audit(
        before: Proposal,
        after: Proposal,
        action: ProposalAction,
        authority: AuthorityContext,
        processed_at: datetime,
        request_fingerprint: str,
    ) -> tuple[ProposalActionResult, ProposalActionAuditEvent]:
        event_seed = (
            f"{action.idempotency_key}:{request_fingerprint}:{after.status.value}:{after.revision}"
        )
        event_id = f"AUD-{hashlib.sha256(event_seed.encode()).hexdigest()[:16].upper()}"
        digest = proposal_digest(after)
        audit = ProposalActionAuditEvent(
            event_id=event_id,
            event_type=f"proposal.{action.action.value}",
            proposal_ref=action.proposal_ref,
            actor_ref=action.actor_ref,
            channel_ref=action.channel_ref,
            before_status=before.status,
            after_status=after.status,
            expected_version=action.expected_version,
            proposal_digest=action.expected_digest,
            request_id=authority.source.request_id,
            idempotency_key=action.idempotency_key,
            occurred_at=action.occurred_at,
            processed_at=processed_at,
        )
        result = ProposalActionResult(
            proposal_ref=action.proposal_ref,
            proposal_status=after.status,
            proposal_version=after.revision,
            proposal_digest=digest,
            audit_event_id=event_id,
        )
        return result, audit

    def _idempotent_result(
        self,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> ProposalActionResult | None:
        stored = self.ledger.get(idempotency_key)
        if stored is None:
            return None
        if stored.request_fingerprint != request_fingerprint:
            raise ProposalActionError(
                "IDEMPOTENCY_CONFLICT", "같은 key에 다른 action payload가 제출됐습니다."
            )
        return stored.result

    @staticmethod
    def _request_fingerprint(action: ProposalAction) -> str:
        payload = json.dumps(
            action.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                self.lock_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            raise ProposalActionError(
                "ACTION_BUSY", "다른 Proposal action이 진행 중입니다."
            ) from None
        try:
            yield
        finally:
            os.close(descriptor)
            self.lock_path.unlink(missing_ok=True)
