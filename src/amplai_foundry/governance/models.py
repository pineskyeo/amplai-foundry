"""Qualified actor, channel, authority, and Proposal action models."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.proposals.models import ProposalId, ProposalStatus

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ActorId = Annotated[str, StringConstraints(pattern=r"^ACT-[A-Z0-9-]+$")]
ActionToken = Annotated[str, StringConstraints(strip_whitespace=True, min_length=8, max_length=256)]
Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]


class ActorType(StrEnum):
    HUMAN = "human"
    SERVICE = "service"
    AGENT = "agent"


class ActorRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: ActorId
    actor_type: ActorType


class ActorBindingStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class ChannelProvider(StrEnum):
    SLACK = "slack"
    TELEGRAM = "telegram"
    HERMES = "hermes"
    WEB = "web"
    CLI = "cli"


class ExternalActorBinding(BaseModel):
    """Immutable provider identity mapped to one AMPLAI actor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ChannelProvider
    workspace_id: NonEmptyString | None = None
    external_actor_id: NonEmptyString
    actor_ref: ActorRef
    status: ActorBindingStatus = ActorBindingStatus.ACTIVE

    @model_validator(mode="after")
    def validate_provider_shape(self) -> ExternalActorBinding:
        if self.provider is ChannelProvider.SLACK and self.workspace_id is None:
            raise ValueError("Slack actor binding에는 workspace_id가 필요합니다.")
        if self.provider is ChannelProvider.TELEGRAM and self.workspace_id is not None:
            raise ValueError("Telegram actor binding에는 workspace_id를 사용하지 않습니다.")
        return self


class ChannelRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ChannelProvider
    workspace_id: NonEmptyString | None = None
    channel_id: NonEmptyString | None = None
    chat_id: NonEmptyString | None = None
    thread_id: NonEmptyString | None = None
    message_id: NonEmptyString

    @model_validator(mode="after")
    def validate_provider_shape(self) -> ChannelRef:
        if self.provider is ChannelProvider.SLACK and (
            self.workspace_id is None or self.channel_id is None or self.chat_id is not None
        ):
            raise ValueError("Slack ChannelRef에는 workspace_id와 channel_id가 필요합니다.")
        if self.provider is ChannelProvider.TELEGRAM and (
            self.chat_id is None or self.workspace_id is not None
        ):
            raise ValueError("Telegram ChannelRef에는 chat_id가 필요합니다.")
        return self


class AuthorityPermission(StrEnum):
    PROPOSAL_READ = "proposal.read"
    PROPOSAL_SUBMIT_REVIEW = "proposal.submit_review"
    PROPOSAL_DECIDE = "proposal.decide"
    PROPOSAL_REQUEST_APPLY = "proposal.request_apply"
    PROPOSAL_APPLY_EXECUTE = "proposal.apply.execute"
    AUTHORITY_BINDING_MANAGE = "authority.binding.manage"
    ACTIVATION_MANAGE = "activation.manage"
    # Legacy v1 permission kept until MGC-007 closes old action paths.
    PROPOSAL_APPLY = "proposal.apply"


class AuthoritySource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: NonEmptyString
    channel: ChannelRef


class AuthorityContext(BaseModel):
    """Server-created authority resolved from actor, project, binding, and policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_ref: ActorRef
    project_ref: ProjectRef
    permissions: frozenset[AuthorityPermission] = Field(min_length=1)
    source: AuthoritySource
    authenticated_at: AwareDatetime


class ProposalRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_ref: ProjectRef
    proposal_id: ProposalId


class ProposalActionType(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGES = "request_changes"
    APPLY = "apply"


class ProposalAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    proposal_ref: ProposalRef
    expected_version: int = Field(ge=1)
    expected_digest: Digest
    action: ProposalActionType
    actor_ref: ActorRef
    channel_ref: ChannelRef
    action_token: ActionToken
    idempotency_key: NonEmptyString
    occurred_at: AwareDatetime


class ProposalActionAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: Annotated[str, StringConstraints(pattern=r"^AUD-[A-F0-9]{16}$")]
    event_type: NonEmptyString
    proposal_ref: ProposalRef
    actor_ref: ActorRef
    channel_ref: ChannelRef
    before_status: ProposalStatus
    after_status: ProposalStatus
    expected_version: int
    proposal_digest: Digest
    request_id: NonEmptyString
    idempotency_key: NonEmptyString
    occurred_at: AwareDatetime
    processed_at: AwareDatetime


class ProposalActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    result: Literal["accepted"] = "accepted"
    proposal_ref: ProposalRef
    proposal_status: ProposalStatus
    proposal_version: int
    proposal_digest: Digest
    audit_event_id: NonEmptyString


class StoredProposalAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_fingerprint: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    result: ProposalActionResult
    audit: ProposalActionAuditEvent
