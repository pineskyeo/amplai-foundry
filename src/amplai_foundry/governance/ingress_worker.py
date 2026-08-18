"""Background decision handoff for durably accepted Provider commands."""

from __future__ import annotations

import sqlite3
from enum import StrEnum
from typing import Protocol

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
    is_store_corruption,
)

_LEASE_CONFLICT = "INGRESS_LEASE_CONFLICT"

# 소진됐지만 **결정은 이미 기록된** 종점. 사용자 통지를 만들지 않는다.
#
# 그 결정의 Result Card 는 이미 나갔으므로 여기서 실패를 알리면 배달된 Card 와 모순된다.
# D-034 도 성공한 첫 결정은 safe outcome 을 만들지 않는다고 못박았다. 그래서 침묵이지만
# **명령은 여전히 recovery hold 로 남아** operator 목록에 보인다 — 결정은 됐는데 장부
# 정리가 안 된 상태라 사람이 한 번 봐야 한다.
_DECISION_COMMITTED_UNRECONCILED = "INGRESS_DECISION_COMMITTED_UNRECONCILED"
_SILENT_HOLD_CODES: frozenset[str] = frozenset({_DECISION_COMMITTED_UNRECONCILED})

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
        "ACTION_TOKEN_REVOKED",
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

_DENIED_CODES: frozenset[str] = frozenset(
    {
        "ACTION_ACTOR_MISMATCH",
        "ACTION_CHANNEL_MISMATCH",
        "ACTION_TOKEN_INVALID",
        "ACTOR_DISABLED",
        "ACTOR_UNMAPPED",
        "AUTHORITY_BOOTSTRAP_CLOSED",
        "AUTHORITY_DENIED",
        "BINDING_STALE",
        "PROJECT_ACCESS_DENIED",
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


class SafeInteractionOutcome(StrEnum):
    """Closed user-visible outcomes; internal policy detail never crosses this port.

    성공한 첫 결정은 여기에 없다. 그 결과는 별도 Result Card 가 알린다
    (`contracts/interaction-feedback.md` Delivery, FR-013). producer 없는 값을 남기면
    다음 사람이 이미 보내고 있다고 읽거나 producer 를 붙여 같은 사건을 두 번 알린다 (D-034).
    """

    ALREADY_COMPLETED = "already_completed"
    EXPIRED = "expired"
    STALE = "stale"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


class InteractionFeedback(Protocol):
    """Optional non-authoritative feedback sent after durable ingress finalization."""

    def send(
        self,
        command: IngressCommandView,
        outcome: SafeInteractionOutcome,
    ) -> None: ...


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
    feedback_error_code: str | None = None
    decision: DecisionResult | None = None


class IngressDecisionWorker:
    """Resolve leased ingress commands through the governed decision contract."""

    def __init__(
        self,
        store: GovernanceStore,
        ingress: IngressService,
        decisions: DecisionService,
        feedback: InteractionFeedback | None = None,
    ) -> None:
        self.store = store
        self.ingress = ingress
        self.decisions = decisions
        self.feedback = feedback

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
            return self._with_feedback(
                claim,
                self._finalize(claim, worker_id, outcome, error_code=error.code),
            )
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
            return self._with_feedback(
                claim,
                self._finalize(claim, worker_id, outcome, error_code=error.code),
            )
        except sqlite3.DatabaseError as error:
            # A corrupt database fails closed. SPEC requires no further governed
            # mutation attempts once storage integrity is in doubt.
            if is_store_corruption(error):
                return self._with_feedback(
                    claim,
                    self._finalize(
                        claim,
                        worker_id,
                        WorkerOutcome.RECOVERY_HOLD,
                        error_code="INGRESS_STORE_CORRUPT",
                    ),
                )
            return self._with_feedback(
                claim,
                self._finalize(
                    claim,
                    worker_id,
                    WorkerOutcome.RETRY,
                    error_code="INGRESS_DECISION_UNAVAILABLE",
                ),
            )
        except (GovernanceStoreError, sqlite3.Error):
            return self._with_feedback(
                claim,
                self._finalize(
                    claim,
                    worker_id,
                    WorkerOutcome.RETRY,
                    error_code="INGRESS_DECISION_UNAVAILABLE",
                ),
            )
        except Exception:
            # Fail closed on an unclassified error rather than abandoning the lease.
            return self._with_feedback(
                claim,
                self._finalize(
                    claim,
                    worker_id,
                    WorkerOutcome.RETRY,
                    error_code="INGRESS_DECISION_FAILED",
                ),
            )
        return self._with_feedback(
            claim,
            self._finalize(
                claim,
                worker_id,
                WorkerOutcome.COMPLETED,
                decision=decision,
            ),
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

        The result is bound to the command's own request fingerprint, credential and
        action, so a foreign row stored under the same replay key answers None instead
        of misreporting. Two clicks on one token produce distinct commands with distinct
        fingerprints, which is why the fingerprint carries the binding.
        """

        command = self.ingress.get(command_id)
        if command is None:
            return None
        result = self.decisions.result_for(
            self.idempotency_key(command_id),
            expected_fingerprint=command.provider_fingerprint,
        )
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
        outcome, error_code = self._settle_exhausted_retry(claim, outcome, error_code)
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

    def _with_feedback(
        self,
        claim: IngressCommandView,
        result: IngressWorkerResult,
    ) -> IngressWorkerResult:
        """Send best-effort feedback only after the ingress transition is durable."""

        if self.feedback is None or result.state is None:
            return result
        safe_outcome = self._safe_outcome(result)
        if safe_outcome is None:
            return result
        try:
            self.feedback.send(claim, safe_outcome)
        except Exception:
            return result.model_copy(update={"feedback_error_code": "INTERACTION_FEEDBACK_FAILED"})
        return result

    @staticmethod
    def _safe_outcome(result: IngressWorkerResult) -> SafeInteractionOutcome | None:
        if (
            result.outcome is WorkerOutcome.COMPLETED
            and result.decision is not None
            and result.decision.replayed
        ):
            return SafeInteractionOutcome.ALREADY_COMPLETED
        if result.outcome is not WorkerOutcome.RECOVERY_HOLD:
            return None
        if result.error_code in _SILENT_HOLD_CODES:
            # **알리지 않는다. 이유가 둘이고 결과는 같다** (`D-039`, `D-042`).
            #
            # 결정이 기록된 것을 확인한 경우 — 이미 알렸다. Result Card 가 했다.
            # 확인하지 못한 경우 — 알렸는지 모른다. 모르는 채로 실패를 알리면 배달된 Card 와
            # 모순될 수 있고 그 통지는 되돌릴 수 없다.
            #
            # 둘 다 `stranded()` 에 남으므로 operator 가 회수한다.
            return None
        if result.error_code == "ACTION_TOKEN_CONSUMED":
            return SafeInteractionOutcome.ALREADY_COMPLETED
        if result.error_code == "ACTION_TOKEN_EXPIRED":
            return SafeInteractionOutcome.EXPIRED
        if result.error_code in {"PROPOSAL_STALE", "INVALID_PROPOSAL_STATE"}:
            return SafeInteractionOutcome.STALE
        if result.error_code == "ACTION_TOKEN_REVOKED":
            return SafeInteractionOutcome.STALE
        if result.error_code in _DENIED_CODES:
            return SafeInteractionOutcome.DENIED
        return SafeInteractionOutcome.UNAVAILABLE

    def _settle_exhausted_retry(
        self,
        claim: IngressCommandView,
        outcome: WorkerOutcome,
        error_code: str | None,
    ) -> tuple[WorkerOutcome, str | None]:
        """Turn the last attempt's retry into a terminal recovery hold.

        재시도로 끝난 마지막 시도를 그대로 두면 그 command 는 영원히 조용해진다.
        `claim_next` 의 sweep 이 `retry_wait` + 소진을 `dead_letter` 로 옮기고, claim 조건은
        `attempts < max_attempts` 라 그 row 를 다시 claim 하지 않는다. worker 가 다시 돌지
        않으므로 feedback 도 돌지 않는다. reviewer 는 HTTP 200 을 받은 뒤 아무것도 못 받는다
        (round 11 `R-3`).

        **`claim.attempts` 는 이번 시도를 이미 포함한다.** `claim_next` 가 claim 시
        `attempts = attempts + 1` 하고 그 row 를 다시 읽는다. 그래서 마지막 시도는
        `attempts >= max_attempts` 로 정확히 판별된다.

        승격해도 새 outcome 값이 필요 없다. 소진된 command 는 **진짜** recovery hold 가
        되므로 계약의 `recovery hold without a more specific public result` 정의에 그대로
        맞고 (D-034), `stranded()` 는 `recovery_hold` 도 포함하므로 operator 가시성이
        유지된다.

        `RETRY` 만 승격한다. `COMPLETED`, `RECOVERY_HOLD`, `LEASE_LOST` 는 이미 종결이다.

        **결정이 이미 기록됐는지 먼저 본다.** 앞선 attempt 가 decision 을 commit 하고 완료
        도장을 못 찍은 채 죽으면, 마지막 attempt 가 실패해도 그 결정은 살아 있고 Result Card
        는 이미 나갔다. 거기에 `unavailable` 을 보내면 배달된 Card 와 모순된다 — round 12
        `F-1` 이 실측한 경우다. 이것은 lease 만료 종점에 통지를 넣지 않은 이유(`D-038`
        항목 6)와 **같은 이유**이고, 승격 분기가 그것을 보지 않은 것이 결함이었다.

        그때도 승격은 한다. 명령이 recovery hold 로 남아야 `stranded()` 에 보이고, 결정은
        됐는데 장부가 안 맞는 상태를 사람이 확인할 수 있다. 다만 code 를 바꿔
        `_safe_outcome` 이 침묵하게 한다.

        **그 읽기가 실패하는 경우도 침묵이다 (`D-042`).** `committed_decision()` 은
        connection 두 개를 새로 연다. 이 호출은 `_finalize` 의 `try` 밖이라 예외가
        `process_next` 를 뚫고 나갔고, command 는 `attempts == max_attempts` 인 채 `leased`
        로 남았다. claim 조건이 `attempts < max_attempts` 라 그 row 는 다시 잡히지 않는다 —
        reviewer 는 아무것도 못 받고 worker loop 를 도는 caller 도 죽는다 (round 13 `P1-1`).

        **이것은 상관된 실패다.** 재시도를 소진시킨 그 조건(store busy)이 이 읽기도
        실패시킨다. 드물어서 넘길 수 있는 종류가 아니다.

        모르는 상태에서 `unavailable` 을 보내지 않는 이유는 위와 같다 — 결정이 이미
        commit 됐으면 배달된 Result Card 와 모순되고, 그 통지는 되돌릴 수 없다. 침묵은
        `stranded()` 로 회수할 수 있다. 되돌릴 수 있는 쪽을 고른다.

        **`except` 를 넓히지 않는다.** `_finalize` 가 store 실패로 잡는 것과 같은 집합이다.
        `Exception` 으로 덮으면 프로그래밍 오류까지 침묵이 되어 결함이 durable 하게 숨는다.
        """
        if outcome is not WorkerOutcome.RETRY:
            return outcome, error_code
        if claim.attempts < self.ingress.config.max_attempts:
            return outcome, error_code
        try:
            committed = self.committed_decision(claim.command_id)
        except (GovernanceStoreError, IngressError, sqlite3.Error):
            return WorkerOutcome.RECOVERY_HOLD, _DECISION_COMMITTED_UNRECONCILED
        if committed is not None:
            return WorkerOutcome.RECOVERY_HOLD, _DECISION_COMMITTED_UNRECONCILED
        return WorkerOutcome.RECOVERY_HOLD, error_code

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


def _error_code(error: Exception, fallback: str) -> str:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and code else fallback
