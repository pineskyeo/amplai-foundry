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
from amplai_foundry.governance.events import GovernanceEventError
from amplai_foundry.governance.ingress import (
    IngressCommandView,
    IngressError,
    IngressLeaseConflictError,
    IngressService,
    IngressState,
)
from amplai_foundry.governance.store import (
    GovernanceStore,
    GovernanceStoreError,
    governance_transaction,
)

_LEASE_CONFLICT = "INGRESS_LEASE_CONFLICT"

# Event errors are integrity assertions by default, so an unlisted code is terminal.
# Only sequence and revision CAS losses can change on a later attempt.
_RETRYABLE_EVENT_CODES: frozenset[str] = frozenset(
    {
        "AUDIT_SEQUENCE_CONFLICT",
        "OUTBOX_SEQUENCE_CONFLICT",
        "OUTBOX_SOURCE_REVISION_CONFLICT",
    }
)

# Denials that no retry can change. Every other failure keeps its retry budget.
# Covers AuthorityResolutionError and DecisionError, including the legacy gate codes
# those surface. GovernanceEventError is classified by _RETRYABLE_EVENT_CODES instead.
_TERMINAL_CODES: frozenset[str] = frozenset(
    {
        "ACTION_ACTOR_MISMATCH",
        "ACTION_CHANNEL_MISMATCH",
        "ACTION_TOKEN_CONSUMED",
        "ACTION_TOKEN_EXPIRED",
        "ACTION_TOKEN_INVALID",
        "ACTOR_DISABLED",
        "ACTOR_UNMAPPED",
        "AUTHORITY_BOOTSTRAP_CLOSED",
        "AUTHORITY_DENIED",
        "BINDING_STALE",
        "GOVERNANCE_TRANSACTION_REQUIRED",
        "IDEMPOTENCY_CONFLICT",
        "INVALID_PROPOSAL_STATE",
        "LEGACY_APPROVAL_REVIEW_REQUIRED",
        "LEGACY_FORWARD_RECOVERY_REQUIRED",
        "LEGACY_MIGRATION_EVENT_BACKFILL_PENDING",
        "LEGACY_MIGRATION_NOT_ACTIVATED",
        "LEGACY_MIGRATION_RECOVERY_REQUIRED",
        "LEGACY_MIGRATION_ROLLED_BACK",
        "PROJECT_ACCESS_DENIED",
        "PROPOSAL_NOT_FOUND",
        "PROPOSAL_STALE",
        "PROVIDER_INSTALLATION_INVALID",
    }
)


class WorkerOutcome(StrEnum):
    COMPLETED = "completed"
    RETRY = "retry"
    RECOVERY_HOLD = "recovery_hold"
    LEASE_LOST = "lease_lost"
    FINALIZE_FAILED = "finalize_failed"
    CLAIM_FAILED = "claim_failed"


class IngressWorkerResult(BaseModel):
    """One background handoff attempt for a single durable ingress command.

    `error_code` reports the decision phase and `finalize_error_code` the ingress write
    that followed it. A lease lost before the decision therefore carries the lease code
    in `error_code`; a lease lost during the write carries it in `finalize_error_code`
    and keeps the decision outcome. Read `finalize_error_code` to detect write failures
    uniformly. `decision` is set only when the decision transaction committed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: WorkerOutcome
    command_id: str | None = None
    state: IngressState | None = None
    error_code: str | None = None
    finalize_error_code: str | None = None
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
        """Claim one command and resolve it.

        Returns None only when nothing is claimable. Store and ingress failures are
        reported as `CLAIM_FAILED` rather than raised, so a caller loop survives them.
        A malformed `worker_id` is a caller defect and still raises `ValueError`.
        """

        if not worker_id.strip():
            raise ValueError("worker_id는 비어 있을 수 없습니다.")
        try:
            claim = self.ingress.claim_next(worker_id)
        except (GovernanceStoreError, IngressError, sqlite3.Error) as error:
            return IngressWorkerResult(
                outcome=WorkerOutcome.CLAIM_FAILED,
                error_code=_error_code(error, "INGRESS_CLAIM_FAILED"),
            )
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
        except GovernanceEventError as error:
            outcome = (
                WorkerOutcome.RETRY
                if error.code in _RETRYABLE_EVENT_CODES
                else WorkerOutcome.RECOVERY_HOLD
            )
            return self._finalize(claim, worker_id, outcome, error_code=error.code)
        except (AuthorityResolutionError, DecisionError) as error:
            if error.code == _LEASE_CONFLICT:
                return IngressWorkerResult(
                    outcome=WorkerOutcome.LEASE_LOST,
                    command_id=claim.command_id,
                    error_code=error.code,
                )
            outcome = (
                WorkerOutcome.RECOVERY_HOLD
                if error.code in _TERMINAL_CODES
                else WorkerOutcome.RETRY
            )
            return self._finalize(claim, worker_id, outcome, error_code=error.code)
        except sqlite3.DatabaseError as error:
            # A corrupt database fails closed. SPEC requires no further governed
            # mutation attempts once storage integrity is in doubt.
            if _is_corruption(error):
                return self._finalize(
                    claim,
                    worker_id,
                    WorkerOutcome.RECOVERY_HOLD,
                    error_code="INGRESS_STORE_CORRUPT",
                )
            return self._finalize(
                claim,
                worker_id,
                WorkerOutcome.RETRY,
                error_code="INGRESS_DECISION_UNAVAILABLE",
            )
        except (GovernanceStoreError, sqlite3.Error):
            return self._finalize(
                claim,
                worker_id,
                WorkerOutcome.RETRY,
                error_code="INGRESS_DECISION_UNAVAILABLE",
            )
        except Exception:
            # Fail closed on an unclassified error rather than abandoning the lease.
            return self._finalize(
                claim,
                worker_id,
                WorkerOutcome.RETRY,
                error_code="INGRESS_DECISION_FAILED",
            )
        return self._finalize(
            claim,
            worker_id,
            WorkerOutcome.COMPLETED,
            decision=decision,
        )

    @staticmethod
    def idempotency_key(command_id: str) -> str:
        """Derive the replay key from durable identity so a reclaim converges."""

        return f"ingress:{command_id}"

    def committed_decision(self, command_id: str) -> DecisionResult | None:
        """Answer whether a stranded command already decided. Reads no Proposal state.

        A command whose finalize write kept failing ends in `dead_letter` carrying
        `INGRESS_LEASE_EXPIRED`, which reads like a failure even though the governed
        decision committed. Operator recovery uses this to tell the two apart.

        The result is bound to the command's own credential and action, so a foreign
        row stored under the same replay key answers None instead of misreporting.
        """

        command = self.ingress.get(command_id)
        if command is None:
            return None
        result = self.decisions.result_for(self.idempotency_key(command_id))
        if result is None:
            return None
        if result.token_id != command.credential_id or result.action is not command.action:
            return None
        return result

    def _finalize(
        self,
        claim: IngressCommandView,
        worker_id: str,
        outcome: WorkerOutcome,
        *,
        error_code: str | None = None,
        decision: DecisionResult | None = None,
    ) -> IngressWorkerResult:
        try:
            view = self._transition(claim, worker_id, outcome, error_code)
        except IngressLeaseConflictError as error:
            return IngressWorkerResult(
                outcome=WorkerOutcome.LEASE_LOST,
                command_id=claim.command_id,
                error_code=error_code,
                finalize_error_code=error.code,
                decision=decision,
            )
        except (GovernanceStoreError, IngressError, sqlite3.Error) as error:
            # The decision transaction already resolved. The lease expires and a reclaim
            # converges on the first result through the idempotency key. `error_code`
            # keeps the decision outcome so the write failure does not erase it.
            return IngressWorkerResult(
                outcome=WorkerOutcome.FINALIZE_FAILED,
                command_id=claim.command_id,
                error_code=error_code,
                finalize_error_code=_error_code(error, "INGRESS_FINALIZE_FAILED"),
                decision=decision,
            )
        return IngressWorkerResult(
            outcome=outcome,
            command_id=claim.command_id,
            state=view.state,
            error_code=error_code,
            decision=decision,
        )

    def _transition(
        self,
        claim: IngressCommandView,
        worker_id: str,
        outcome: WorkerOutcome,
        error_code: str | None,
    ) -> IngressCommandView:
        if outcome is WorkerOutcome.COMPLETED:
            return self.ingress.complete(
                claim.command_id,
                worker_id=worker_id,
                generation=claim.claim_generation,
            )
        code = error_code or "INGRESS_DECISION_FAILED"
        if outcome is WorkerOutcome.RECOVERY_HOLD:
            return self.ingress.recovery_hold(
                claim.command_id,
                worker_id=worker_id,
                generation=claim.claim_generation,
                error_code=code,
            )
        return self.ingress.retry(
            claim.command_id,
            worker_id=worker_id,
            generation=claim.claim_generation,
            error_code=code,
        )


def _is_corruption(error: sqlite3.DatabaseError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if not isinstance(code, int):
        return False
    # sqlite_errorcode carries the extended code, so compare the primary byte.
    return code & 0xFF in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}


def _error_code(error: Exception, fallback: str) -> str:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and code else fallback
