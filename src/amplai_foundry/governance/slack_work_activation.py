"""Signed Slack ingress adapter for the separate Work activation aggregate.

The adapter deliberately reuses the strict raw-request verifier but does not
reuse Proposal ingress or its tables.  A Work card's token scope is recovered
from the activation ledger, never from Slack-visible button metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar, Protocol

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance.authority import AuthorityService, DirectAuthorityRequest
from amplai_foundry.governance.decisions import DecisionAction, clear_exception_frames
from amplai_foundry.governance.ingress import ProviderEnvelope
from amplai_foundry.governance.models import AuthorityContext, ChannelRef
from amplai_foundry.governance.slack import SlackBlockActionAuthenticator
from amplai_foundry.governance.work_activation import (
    DurableWorkActivationLedger,
    WorkActivationActionType,
    WorkActivationResult,
    WorkActivationService,
)


@dataclass(frozen=True, slots=True)
class VerifiedSlackWorkActivation:
    """Verified identity facts and a raw credential retained only for this call."""

    external_event_id: str
    external_actor_id: str
    action: WorkActivationActionType
    token_id: str
    raw_token: str
    channel_ref: ChannelRef
    provider_installation_ref: str


class SlackActivationAuthorityResolver(Protocol):
    """Resolve the signed Slack actor through the server-side Actor binding."""

    def resolve(
        self,
        *,
        external_actor_id: str,
        provider_installation_ref: str,
        project_ref: ProjectRef,
        request_id: str,
        channel_ref: ChannelRef,
    ) -> AuthorityContext: ...


class AuthorityServiceSlackResolver:
    """Production resolver; caller never supplies a Slack actor as authority."""

    def __init__(self, authority_service: AuthorityService) -> None:
        self.authority_service = authority_service

    def resolve(
        self,
        *,
        external_actor_id: str,
        provider_installation_ref: str,
        project_ref: ProjectRef,
        request_id: str,
        channel_ref: ChannelRef,
    ) -> AuthorityContext:
        return self.authority_service.authenticate(
            DirectAuthorityRequest(
                provider=channel_ref.provider,
                provider_installation_ref=provider_installation_ref,
                external_actor_id=external_actor_id,
                project_ref=project_ref,
                request_id=request_id,
                channel=channel_ref,
            )
        )


class SlackWorkActivationAuthenticator:
    """Convert an AMPLAI Slack Work-card click into a narrow activation command.

    `SlackInstallationPolicy.action_ids` must contain only the three dedicated
    Work action IDs.  The underlying parser verifies the HMAC before parsing the
    button or exposing its credential.
    """

    _ACTION_IDS: ClassVar[dict[str, DecisionAction]] = {
        "amplai_work_approve": DecisionAction.APPROVE,
        "amplai_work_reject": DecisionAction.REJECT,
        "amplai_work_request_changes": DecisionAction.REQUEST_CHANGES,
    }

    def __init__(self, authenticator: SlackBlockActionAuthenticator) -> None:
        if dict(authenticator.policy.action_ids) != self._ACTION_IDS:
            raise ValueError("Slack Work activation requires its dedicated action ID mapping.")
        self._authenticator = authenticator

    def verify(self, envelope: ProviderEnvelope) -> VerifiedSlackWorkActivation:
        verified = self._authenticator.verify(envelope)
        return VerifiedSlackWorkActivation(
            external_event_id=verified.external_event_id,
            external_actor_id=verified.external_actor_key,
            action=WorkActivationActionType(verified.action.value),
            token_id=verified.credential_id,
            raw_token=verified.raw_credential.get_secret_value(),
            channel_ref=verified.channel_ref,
            provider_installation_ref=envelope.provider_installation_ref,
        )


class SlackWorkActivationIngress:
    """Apply a signed Work card click under already-resolved human authority."""

    def __init__(
        self,
        authenticator: SlackWorkActivationAuthenticator,
        ledger: DurableWorkActivationLedger,
        service: WorkActivationService,
        authority_resolver: SlackActivationAuthorityResolver,
    ) -> None:
        self.authenticator = authenticator
        self.ledger = ledger
        self.service = service
        self.authority_resolver = authority_resolver

    def apply(self, envelope: ProviderEnvelope) -> WorkActivationResult:
        raw_token = ""
        try:
            command = self.authenticator.verify(envelope)
            raw_token = command.raw_token
            action = self.ledger.action_for(
                token_id=command.token_id,
                raw_token=raw_token,
                action=command.action,
                idempotency_key=command.external_event_id,
                occurred_at=self._auth_time(),
            )
            authority = self.authority_resolver.resolve(
                external_actor_id=command.external_actor_id,
                provider_installation_ref=command.provider_installation_ref,
                project_ref=action.project_ref,
                request_id=command.external_event_id,
                channel_ref=command.channel_ref,
            )
            return self.service.apply(action, authority)
        except BaseException as error:
            clear_exception_frames(error)
            raise
        finally:
            raw_token = ""

    @staticmethod
    def _auth_time() -> datetime:
        return datetime.now(UTC)
