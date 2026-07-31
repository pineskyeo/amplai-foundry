"""Bounded synchronous Provider ack with fail-closed response mapping."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field

from amplai_foundry.governance.ingress import (
    IngressError,
    IngressService,
    IngressState,
    ProviderEnvelope,
)

_SUCCESS_STATUS = 200
_UNAVAILABLE_STATUS = 503
_DEFAULT_REJECT_STATUS = 400

_REJECT_STATUS: Mapping[str, int] = MappingProxyType(
    {
        "SLACK_SIGNATURE_MISSING": 401,
        "SLACK_SIGNATURE_INVALID": 401,
        "SLACK_TIMESTAMP_INVALID": 401,
        "SLACK_TIMESTAMP_STALE": 401,
        "SLACK_HEADER_INVALID": 401,
        "SLACK_CREDENTIAL_INVALID": 401,
        "ACTION_TOKEN_INVALID": 401,
        "SLACK_PROVIDER_MISMATCH": 403,
        "SLACK_INSTALLATION_DENIED": 403,
        "SLACK_APP_DENIED": 403,
        "PROVIDER_SCOPE_MISMATCH": 403,
        "PROVIDER_INSTALLATION_INVALID": 403,
        "SLACK_PAYLOAD_INVALID": 400,
        "SLACK_PAYLOAD_UNSUPPORTED": 400,
        "SLACK_ACTION_UNSUPPORTED": 400,
        "PROVIDER_PAYLOAD_INVALID": 400,
        "SLACK_PAYLOAD_TOO_LARGE": 413,
        "INGRESS_PAYLOAD_TOO_LARGE": 413,
        "INGRESS_REPLAY_CONFLICT": 409,
    }
)


class AckOutcome(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    BUDGET_EXCEEDED = "budget_exceeded"


class ProviderAckResponse(BaseModel):
    """One synchronous Provider response. Only `accepted` and `duplicate` are success."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: AckOutcome
    status_code: int = Field(ge=200, le=599)
    elapsed_ms: int = Field(ge=0)
    command_id: str | None = None
    state: IngressState | None = None
    error_code: str | None = None

    @property
    def success(self) -> bool:
        return self.outcome in {AckOutcome.ACCEPTED, AckOutcome.DUPLICATE}


@dataclass(frozen=True, slots=True)
class AckBudget:
    """Provider ack budget. Slack fails a request without a response in three seconds."""

    total_ms: int = 3_000

    def __post_init__(self) -> None:
        if not 1 <= self.total_ms <= 3_000:
            raise ValueError("ack budget total_ms는 1..3000 범위여야 합니다.")


class BoundedIngressAck:
    """Return one Provider response per request without exceeding the ack budget."""

    def __init__(
        self,
        ingress: IngressService,
        *,
        budget: AckBudget | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.ingress = ingress
        self.budget = budget or AckBudget()
        self._monotonic = monotonic or time.monotonic
        if ingress.config.busy_timeout_ms >= self.budget.total_ms:
            raise ValueError("ingress busy_timeout은 ack budget보다 짧아야 합니다.")

    def submit(self, envelope: ProviderEnvelope) -> ProviderAckResponse:
        started = self._monotonic()
        try:
            ack = self.ingress.accept(envelope)
        except IngressError as error:
            return self._respond(
                AckOutcome.REJECTED,
                started,
                status_code=_REJECT_STATUS.get(error.code, _DEFAULT_REJECT_STATUS),
                error_code=error.code,
            )
        except Exception:
            return self._respond(
                AckOutcome.UNAVAILABLE,
                started,
                status_code=_UNAVAILABLE_STATUS,
                error_code="INGRESS_INTERNAL_ERROR",
            )
        if not ack.accepted:
            return self._respond(
                AckOutcome.UNAVAILABLE,
                started,
                status_code=_UNAVAILABLE_STATUS,
                error_code=ack.error_code or "INGRESS_UNAVAILABLE",
            )
        elapsed = self._elapsed_ms(started)
        if elapsed >= self.budget.total_ms:
            return ProviderAckResponse(
                outcome=AckOutcome.BUDGET_EXCEEDED,
                status_code=_UNAVAILABLE_STATUS,
                elapsed_ms=elapsed,
                command_id=ack.command_id,
                state=ack.state,
                error_code="INGRESS_ACK_BUDGET_EXCEEDED",
            )
        return ProviderAckResponse(
            outcome=AckOutcome.DUPLICATE if ack.duplicate else AckOutcome.ACCEPTED,
            status_code=_SUCCESS_STATUS,
            elapsed_ms=elapsed,
            command_id=ack.command_id,
            state=ack.state,
        )

    def _respond(
        self,
        outcome: AckOutcome,
        started: float,
        *,
        status_code: int,
        error_code: str,
    ) -> ProviderAckResponse:
        return ProviderAckResponse(
            outcome=outcome,
            status_code=status_code,
            elapsed_ms=self._elapsed_ms(started),
            error_code=error_code,
        )

    def _elapsed_ms(self, started: float) -> int:
        return max(int((self._monotonic() - started) * 1_000), 0)
