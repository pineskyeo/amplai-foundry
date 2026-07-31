"""Background decision handoff for durably accepted Provider commands."""

from __future__ import annotations

import sqlite3
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from amplai_foundry.governance.authority import (
    AuthorityResolutionError,
    IngressAuthorityRequest,
)
from amplai_foundry.governance.decisions import (
    DecisionError,
    DecisionResult,
    DecisionService,
)
from amplai_foundry.governance.ingress import (
    IngressCommandView,
    IngressLeaseConflictError,
    IngressService,
    IngressState,
)
from amplai_foundry.governance.store import (
    GovernanceStore,
    GovernanceStoreError,
    governance_transaction,
)

_TERMINAL_CODES: frozenset[str] = frozenset(
    {
        "ACTION_ACTOR_MISMATCH",
        "ACTION_CHANNEL_MISMATCH",
        "ACTION_TOKEN_CONSUMED",
        "ACTION_TOKEN_EXPIRED",
        "ACTION_TOKEN_INVALID",
        "ACTOR_DISABLED",
        "ACTOR_UNMAPPED",
        "AUTHORITY_DENIED",
        "BINDING_STALE",
        "GOVERNANCE_TRANSACTION_REQUIRED",
        "IDEMPOTENCY_CONFLICT",
        "INVALID_PROPOSAL_STATE",
        "PROPOSAL_NOT_FOUND",
        "PROPOSAL_STALE",
    }
)


class WorkerOutcome(StrEnum):
    COMPLETED = "completed"
    RETRY = "retry"
    RECOVERY_HOLD = "recovery_hold"
    LEASE_LOST = "lease_lost"


class IngressWorkerResult(BaseModel):
    """One background handoff attempt for a single durable ingress command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: WorkerOutcome
    command_id: str
    state: IngressState | None = None
    error_code: str | None = None
    decision: DecisionResult | None = None


class IngressDecisionWorker:
    """Resolve leased ingress commands through the governed decision contract."""

    def __init__(
        self,
        store: GovernanceStore,
        ingress: IngressService,
        decisions: DecisionService,
    ) -> None:
        self.store = store
        self.ingress = ingress
        self.decisions = decisions

    def process_next(self, worker_id: str) -> IngressWorkerResult | None:
        claim = self.ingress.claim_next(worker_id)
        if claim is None:
            return None
        return self.process(claim, worker_id=worker_id)

    def process(self, claim: IngressCommandView, *, worker_id: str) -> IngressWorkerResult:
        request = IngressAuthorityRequest(
            command_id=claim.command_id,
            worker_id=worker_id,
            generation=claim.claim_generation,
        )
        try:
            with (
                self.store.connect() as connection,
                governance_transaction(connection),
            ):
                decision = self.decisions.decide_ingress_in_transaction(
                    connection,
                    request,
                    idempotency_key=self.idempotency_key(claim.command_id),
                    request_fingerprint=claim.provider_fingerprint,
                )
        except (AuthorityResolutionError, DecisionError) as error:
            if error.code == "INGRESS_LEASE_CONFLICT":
                return IngressWorkerResult(
                    outcome=WorkerOutcome.LEASE_LOST,
                    command_id=claim.command_id,
                    error_code=error.code,
                )
            if error.code in _TERMINAL_CODES:
                return self._hold(claim, worker_id=worker_id, error_code=error.code)
            return self._retry(claim, worker_id=worker_id, error_code=error.code)
        except (GovernanceStoreError, sqlite3.Error):
            return self._retry(
                claim,
                worker_id=worker_id,
                error_code="INGRESS_DECISION_UNAVAILABLE",
            )
        return self._complete(claim, worker_id=worker_id, decision=decision)

    @staticmethod
    def idempotency_key(command_id: str) -> str:
        """Derive the replay key from durable identity so a reclaim converges."""

        return f"ingress:{command_id}"

    def _complete(
        self,
        claim: IngressCommandView,
        *,
        worker_id: str,
        decision: DecisionResult,
    ) -> IngressWorkerResult:
        try:
            view = self.ingress.complete(
                claim.command_id,
                worker_id=worker_id,
                generation=claim.claim_generation,
            )
        except IngressLeaseConflictError as error:
            return IngressWorkerResult(
                outcome=WorkerOutcome.LEASE_LOST,
                command_id=claim.command_id,
                error_code=error.code,
                decision=decision,
            )
        return IngressWorkerResult(
            outcome=WorkerOutcome.COMPLETED,
            command_id=claim.command_id,
            state=view.state,
            decision=decision,
        )

    def _retry(
        self,
        claim: IngressCommandView,
        *,
        worker_id: str,
        error_code: str,
    ) -> IngressWorkerResult:
        try:
            view = self.ingress.retry(
                claim.command_id,
                worker_id=worker_id,
                generation=claim.claim_generation,
                error_code=error_code,
            )
        except IngressLeaseConflictError as error:
            return IngressWorkerResult(
                outcome=WorkerOutcome.LEASE_LOST,
                command_id=claim.command_id,
                error_code=error.code,
            )
        return IngressWorkerResult(
            outcome=WorkerOutcome.RETRY,
            command_id=claim.command_id,
            state=view.state,
            error_code=error_code,
        )

    def _hold(
        self,
        claim: IngressCommandView,
        *,
        worker_id: str,
        error_code: str,
    ) -> IngressWorkerResult:
        try:
            view = self.ingress.recovery_hold(
                claim.command_id,
                worker_id=worker_id,
                generation=claim.claim_generation,
                error_code=error_code,
            )
        except IngressLeaseConflictError as error:
            return IngressWorkerResult(
                outcome=WorkerOutcome.LEASE_LOST,
                command_id=claim.command_id,
                error_code=error.code,
            )
        return IngressWorkerResult(
            outcome=WorkerOutcome.RECOVERY_HOLD,
            command_id=claim.command_id,
            state=view.state,
            error_code=error_code,
        )
