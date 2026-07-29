"""Server-side external actor binding and authority resolution."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance.models import (
    ActorBindingStatus,
    ActorRef,
    AuthorityContext,
    AuthorityPermission,
    AuthoritySource,
    ChannelProvider,
    ChannelRef,
    ExternalActorBinding,
)


class AuthorityResolutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code} {message}")


class ExternalActorBindingRepository(Protocol):
    def get(
        self,
        *,
        provider: ChannelProvider,
        workspace_id: str | None,
        external_actor_id: str,
    ) -> ExternalActorBinding | None: ...


class ProjectPermissionPolicy(Protocol):
    def permissions_for(
        self,
        actor_ref: ActorRef,
        project_ref: ProjectRef,
    ) -> frozenset[AuthorityPermission]: ...


class FileExternalActorBindingRepository:
    """Read/write adapter keyed by provider immutable actor identity."""

    def __init__(self, root: Path = Path(".amplai/authority/actor-bindings")) -> None:
        self.root = root

    def path_for(
        self,
        *,
        provider: ChannelProvider,
        workspace_id: str | None,
        external_actor_id: str,
    ) -> Path:
        key = f"{provider.value}:{workspace_id or '-'}:{external_actor_id}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def get(
        self,
        *,
        provider: ChannelProvider,
        workspace_id: str | None,
        external_actor_id: str,
    ) -> ExternalActorBinding | None:
        path = self.path_for(
            provider=provider,
            workspace_id=workspace_id,
            external_actor_id=external_actor_id,
        )
        if not path.exists():
            return None
        try:
            return ExternalActorBinding.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            raise AuthorityResolutionError(
                "ACTOR_BINDING_INVALID", f"Actor binding을 읽을 수 없습니다: {path}"
            ) from error

    def save(self, binding: ExternalActorBinding) -> Path:
        path = self.path_for(
            provider=binding.provider,
            workspace_id=binding.workspace_id,
            external_actor_id=binding.external_actor_id,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(
                binding.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        descriptor = -1
        temporary: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            return path
        except OSError as error:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise AuthorityResolutionError(
                "ACTOR_BINDING_WRITE_FAILED", "Actor binding을 저장할 수 없습니다."
            ) from error


class AuthorityService:
    """Resolve an authenticated external identity into server-created authority."""

    def __init__(
        self,
        bindings: ExternalActorBindingRepository,
        policy: ProjectPermissionPolicy,
    ) -> None:
        self.bindings = bindings
        self.policy = policy

    def authenticate(
        self,
        *,
        provider: ChannelProvider,
        workspace_id: str | None,
        external_actor_id: str,
        project_ref: ProjectRef,
        request_id: str,
        channel: ChannelRef,
        authenticated_at: datetime,
    ) -> AuthorityContext:
        if channel.provider is not provider or channel.workspace_id != workspace_id:
            raise AuthorityResolutionError(
                "ACTION_CHANNEL_MISMATCH", "인증 provider context와 channel이 다릅니다."
            )
        binding = self.bindings.get(
            provider=provider,
            workspace_id=workspace_id,
            external_actor_id=external_actor_id,
        )
        if binding is None:
            raise AuthorityResolutionError("ACTOR_UNMAPPED", "매핑된 Actor가 없습니다.")
        if binding.status is not ActorBindingStatus.ACTIVE:
            raise AuthorityResolutionError("ACTOR_DISABLED", "Actor binding이 비활성입니다.")
        permissions = self.policy.permissions_for(binding.actor_ref, project_ref)
        if not permissions:
            raise AuthorityResolutionError(
                "PROJECT_ACCESS_DENIED", "Actor가 project permission을 갖지 않습니다."
            )
        return AuthorityContext(
            actor_ref=binding.actor_ref,
            project_ref=project_ref,
            permissions=permissions,
            source=AuthoritySource(request_id=request_id, channel=channel),
            authenticated_at=authenticated_at,
        )
