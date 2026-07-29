from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance import (
    ActorBindingStatus,
    ActorRef,
    ActorType,
    AuthorityPermission,
    AuthorityResolutionError,
    AuthorityService,
    ChannelProvider,
    ChannelRef,
    ExternalActorBinding,
    FileExternalActorBindingRepository,
)

NOW = datetime(2026, 7, 29, 17, 30, tzinfo=ZoneInfo("Asia/Seoul"))
PROJECT = ProjectRef(project_id="amplai", namespace="org/default/project/amplai")
ACTOR = ActorRef(actor_id="ACT-USER-1", actor_type=ActorType.HUMAN)
CHANNEL = ChannelRef(
    provider=ChannelProvider.SLACK,
    workspace_id="T123",
    channel_id="C456",
    message_id="1710000000.000200",
)


class FixedProjectPolicy:
    def __init__(self, permissions: frozenset[AuthorityPermission]) -> None:
        self.permissions = permissions

    def permissions_for(
        self,
        actor_ref: ActorRef,
        project_ref: ProjectRef,
    ) -> frozenset[AuthorityPermission]:
        assert actor_ref == ACTOR
        assert project_ref == PROJECT
        return self.permissions


def test_authority_is_created_from_immutable_external_actor_binding(tmp_path: Path) -> None:
    bindings = FileExternalActorBindingRepository(tmp_path / "bindings")
    binding = ExternalActorBinding(
        provider=ChannelProvider.SLACK,
        workspace_id="T123",
        external_actor_id="U456",
        actor_ref=ACTOR,
    )
    bindings.save(binding)
    service = AuthorityService(
        bindings,
        FixedProjectPolicy(frozenset({AuthorityPermission.PROPOSAL_DECIDE})),
    )

    authority = service.authenticate(
        provider=ChannelProvider.SLACK,
        workspace_id="T123",
        external_actor_id="U456",
        project_ref=PROJECT,
        request_id="REQ-001",
        channel=CHANNEL,
        authenticated_at=NOW,
    )

    assert authority.actor_ref == ACTOR
    assert authority.project_ref == PROJECT
    assert authority.permissions == frozenset({AuthorityPermission.PROPOSAL_DECIDE})
    assert (
        bindings.get(
            provider=ChannelProvider.SLACK,
            workspace_id="T123",
            external_actor_id="U456",
        )
        == binding
    )


@pytest.mark.parametrize(
    ("binding_status", "permissions", "error_code"),
    [
        (None, frozenset({AuthorityPermission.PROPOSAL_DECIDE}), "ACTOR_UNMAPPED"),
        (
            ActorBindingStatus.DISABLED,
            frozenset({AuthorityPermission.PROPOSAL_DECIDE}),
            "ACTOR_DISABLED",
        ),
        (ActorBindingStatus.ACTIVE, frozenset(), "PROJECT_ACCESS_DENIED"),
    ],
)
def test_unmapped_disabled_or_unauthorized_actor_is_denied(
    tmp_path: Path,
    binding_status: ActorBindingStatus | None,
    permissions: frozenset[AuthorityPermission],
    error_code: str,
) -> None:
    bindings = FileExternalActorBindingRepository(tmp_path / "bindings")
    if binding_status is not None:
        bindings.save(
            ExternalActorBinding(
                provider=ChannelProvider.SLACK,
                workspace_id="T123",
                external_actor_id="U456",
                actor_ref=ACTOR,
                status=binding_status,
            )
        )
    service = AuthorityService(bindings, FixedProjectPolicy(permissions))

    with pytest.raises(AuthorityResolutionError, match=error_code):
        service.authenticate(
            provider=ChannelProvider.SLACK,
            workspace_id="T123",
            external_actor_id="U456",
            project_ref=PROJECT,
            request_id="REQ-001",
            channel=CHANNEL,
            authenticated_at=NOW,
        )


def test_provider_context_must_match_channel(tmp_path: Path) -> None:
    service = AuthorityService(
        FileExternalActorBindingRepository(tmp_path / "bindings"),
        FixedProjectPolicy(frozenset({AuthorityPermission.PROPOSAL_DECIDE})),
    )

    with pytest.raises(AuthorityResolutionError, match="ACTION_CHANNEL_MISMATCH"):
        service.authenticate(
            provider=ChannelProvider.SLACK,
            workspace_id="another-workspace",
            external_actor_id="U456",
            project_ref=PROJECT,
            request_id="REQ-001",
            channel=CHANNEL,
            authenticated_at=NOW,
        )
