"""Server-side external actor binding and authority resolution."""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance.models import (
    ActorBindingStatus,
    ActorRef,
    ActorType,
    AuthorityContext,
    AuthorityPermission,
    AuthoritySource,
    ChannelProvider,
    ChannelRef,
)
from amplai_foundry.governance.store import GovernanceStore, governance_transaction


class AuthorityResolutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code} {message}")


def _system_now() -> datetime:
    return datetime.now(UTC)


class ActorProfile(StrEnum):
    STANDARD = "standard"
    INTAKE_POLICY = "intake_policy"


class BindingApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str = Field(pattern=r"^APR-[A-F0-9]{16}$")
    approved_by: ActorRef
    reason: str = Field(min_length=3)


class BindingTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ChannelProvider
    provider_installation_ref: str = Field(min_length=1)
    external_actor_id: str = Field(min_length=1)
    actor_ref: ActorRef

    @model_validator(mode="after")
    def validate_installation_scope(self) -> BindingTarget:
        _validate_channel_installation(
            self.provider,
            self.provider_installation_ref,
            None,
            require_channel=False,
        )
        return self


class BindingView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    binding_id: str
    target: BindingTarget
    binding_version: int = Field(ge=1)
    status: ActorBindingStatus
    created_at: AwareDatetime
    disabled_at: AwareDatetime | None = None


class BindingTransitionView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    transition_id: str
    transition_type: str
    provider: ChannelProvider
    provider_installation_ref: str
    external_actor_id: str
    before_binding_id: str | None
    after_binding_id: str | None
    before_actor_id: str | None
    after_actor_id: str | None
    approval_id: str
    approved_by: str
    reason: str
    occurred_at: AwareDatetime


class DirectAuthorityRequest(BaseModel):
    """Server API input; callers supply identity facts, never permissions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ChannelProvider
    provider_installation_ref: str = Field(min_length=1)
    external_actor_id: str = Field(min_length=1)
    project_ref: ProjectRef
    request_id: str = Field(min_length=1)
    channel: ChannelRef


class ExternalActorIdentity(BaseModel):
    """Untrusted identity facts accepted by public ingress and Intake requests."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ChannelProvider
    provider_installation_ref: str = Field(min_length=1)
    external_actor_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    channel: ChannelRef

    def for_project(self, project_ref: ProjectRef) -> DirectAuthorityRequest:
        return DirectAuthorityRequest(
            provider=self.provider,
            provider_installation_ref=self.provider_installation_ref,
            external_actor_id=self.external_actor_id,
            project_ref=project_ref,
            request_id=self.request_id,
            channel=self.channel,
        )


class IngressAuthorityRequest(BaseModel):
    """Worker lease identity. Project and Actor are derived from durable records."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: str = Field(pattern=r"^CMD-[A-F0-9]{16}$")
    worker_id: str = Field(min_length=1)
    generation: int = Field(ge=1)


def _validate_channel_installation(
    provider: ChannelProvider,
    installation: str,
    channel: ChannelRef | None,
    *,
    require_channel: bool = True,
) -> None:
    if not installation.strip():
        raise AuthorityResolutionError(
            "PROVIDER_INSTALLATION_INVALID", "Provider installation이 비어 있습니다."
        )
    if provider is not ChannelProvider.SLACK:
        return
    workspace_id, separator, app_id = installation.partition(":")
    if not workspace_id or not separator or not app_id:
        raise AuthorityResolutionError(
            "PROVIDER_INSTALLATION_INVALID",
            "Slack installation은 workspace_id:app_id 형식이어야 합니다.",
        )
    if require_channel and (channel is None or channel.workspace_id != workspace_id):
        raise AuthorityResolutionError(
            "ACTION_CHANNEL_MISMATCH",
            "Slack installation workspace와 channel workspace가 다릅니다.",
        )


class ActorBindingService:
    """Govern Actor, permission, and external identity binding transitions."""

    _INTAKE_PERMISSIONS = frozenset(
        {
            AuthorityPermission.PROPOSAL_READ,
            AuthorityPermission.PROPOSAL_SUBMIT_REVIEW,
        }
    )

    def __init__(
        self,
        store: GovernanceStore,
        authority_project_ref: ProjectRef,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.authority_project_ref = authority_project_ref
        self._clock = clock or _system_now

    def bootstrap_manager(self, actor_ref: ActorRef) -> None:
        if actor_ref.actor_type is not ActorType.HUMAN:
            raise AuthorityResolutionError("AUTHORITY_DENIED", "bootstrap manager는 human입니다.")
        now = self._timestamp(self._aware(self._clock()))
        with self.store.connect() as connection, governance_transaction(connection):
            if connection.execute("SELECT 1 FROM governance_actors LIMIT 1").fetchone() is not None:
                raise AuthorityResolutionError("AUTHORITY_BOOTSTRAP_CLOSED", "이미 초기화됐습니다.")
            self._insert_actor(connection, actor_ref, ActorProfile.STANDARD, now)
            self._insert_permission(
                connection,
                actor_ref,
                self.authority_project_ref,
                AuthorityPermission.AUTHORITY_BINDING_MANAGE,
                actor_ref.actor_id,
                now,
            )

    def register_actor(
        self,
        actor_ref: ActorRef,
        *,
        profile: ActorProfile = ActorProfile.STANDARD,
        approval: BindingApproval,
    ) -> None:
        now = self._timestamp(self._aware(self._clock()))
        with self.store.connect() as connection, governance_transaction(connection):
            self._authorize_manager(connection, approval)
            self._insert_actor(connection, actor_ref, profile, now)

    def grant_permission(
        self,
        actor_ref: ActorRef,
        project_ref: ProjectRef,
        permission: AuthorityPermission,
        *,
        approval: BindingApproval,
    ) -> None:
        now = self._timestamp(self._aware(self._clock()))
        with self.store.connect() as connection, governance_transaction(connection):
            self._authorize_manager(connection, approval)
            actor_type, profile, status = self._actor_row(connection, actor_ref)
            if status != ActorBindingStatus.ACTIVE.value:
                raise AuthorityResolutionError("ACTOR_DISABLED", "Actor가 비활성입니다.")
            if permission is AuthorityPermission.PROPOSAL_DECIDE and actor_type != ActorType.HUMAN:
                raise AuthorityResolutionError("AUTHORITY_DENIED", "human만 decision 가능합니다.")
            if profile is ActorProfile.INTAKE_POLICY and permission not in self._INTAKE_PERMISSIONS:
                raise AuthorityResolutionError(
                    "AUTHORITY_DENIED", "Intake Policy Actor 권한 범위를 벗어납니다."
                )
            if permission is AuthorityPermission.PROPOSAL_APPLY:
                raise AuthorityResolutionError("AUTHORITY_DENIED", "legacy permission입니다.")
            self._insert_permission(
                connection,
                actor_ref,
                project_ref,
                permission,
                approval.approved_by.actor_id,
                now,
            )

    def disable_actor(
        self,
        actor_ref: ActorRef,
        *,
        approval: BindingApproval,
    ) -> None:
        now = self._timestamp(self._aware(self._clock()))
        with self.store.connect() as connection, governance_transaction(connection):
            self._authorize_manager(connection, approval)
            self._actor_row(connection, actor_ref)
            updated = connection.execute(
                """
                UPDATE governance_actors
                SET status = 'disabled', updated_at = ?
                WHERE actor_id = ? AND actor_type = ? AND status = 'active'
                """,
                (now, actor_ref.actor_id, actor_ref.actor_type.value),
            )
            if updated.rowcount != 1:
                raise AuthorityResolutionError("ACTOR_DISABLED", "Actor가 이미 비활성입니다.")

    def create_binding(
        self,
        target: BindingTarget,
        *,
        approval: BindingApproval,
    ) -> BindingView:
        return self._change_binding("create", target, approval)

    def rebind(
        self,
        target: BindingTarget,
        *,
        approval: BindingApproval,
    ) -> BindingView:
        return self._change_binding("rebind", target, approval)

    def disable_binding(
        self,
        *,
        provider: ChannelProvider,
        provider_installation_ref: str,
        external_actor_id: str,
        approval: BindingApproval,
    ) -> BindingView:
        now_value = self._aware(self._clock())
        now = self._timestamp(now_value)
        with self.store.connect() as connection, governance_transaction(connection):
            self._authorize_manager(connection, approval)
            current = self._active_binding(
                connection, provider, provider_installation_ref, external_actor_id
            )
            if current is None:
                raise AuthorityResolutionError("ACTOR_UNMAPPED", "active binding이 없습니다.")
            self._disable_row(connection, str(current[0]), now)
            self._insert_transition(
                connection,
                transition_type="disable",
                provider=provider,
                installation=provider_installation_ref,
                external_actor_id=external_actor_id,
                before_binding_id=str(current[0]),
                after_binding_id=None,
                before_actor_id=str(current[1]),
                after_actor_id=None,
                approval=approval,
                occurred_at=now,
            )
            return self._binding_view(
                connection, str(current[0]), provider, provider_installation_ref, external_actor_id
            )

    def list_transitions(self) -> tuple[BindingTransitionView, ...]:
        with self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT transition_id, transition_type, provider,
                       provider_installation_ref, external_actor_id,
                       before_binding_id, after_binding_id, before_actor_id,
                       after_actor_id, approval_id, approved_by, reason, occurred_at
                FROM governance_binding_transitions ORDER BY occurred_at, transition_id
                """
            ).fetchall()
        return tuple(
            BindingTransitionView.model_validate(
                dict(
                    zip(
                        BindingTransitionView.model_fields,
                        row,
                        strict=True,
                    )
                )
            )
            for row in rows
        )

    def _change_binding(
        self,
        transition_type: str,
        target: BindingTarget,
        approval: BindingApproval,
    ) -> BindingView:
        now = self._timestamp(self._aware(self._clock()))
        with self.store.connect() as connection, governance_transaction(connection):
            self._authorize_manager(connection, approval)
            _actor_type, _profile, actor_status = self._actor_row(connection, target.actor_ref)
            if actor_status != ActorBindingStatus.ACTIVE.value:
                raise AuthorityResolutionError("ACTOR_DISABLED", "대상 Actor가 비활성입니다.")
            current = self._active_binding(
                connection,
                target.provider,
                target.provider_installation_ref,
                target.external_actor_id,
            )
            latest = self._latest_binding_version(
                connection,
                target.provider,
                target.provider_installation_ref,
                target.external_actor_id,
            )
            if transition_type == "create" and latest != 0:
                raise AuthorityResolutionError(
                    "BINDING_ALREADY_EXISTS", "binding history가 있습니다."
                )
            if transition_type == "rebind" and current is None:
                raise AuthorityResolutionError("ACTOR_UNMAPPED", "active binding이 없습니다.")
            if current is not None and str(current[1]) == target.actor_ref.actor_id:
                raise AuthorityResolutionError("BINDING_UNCHANGED", "Actor가 동일합니다.")
            before_binding_id = str(current[0]) if current is not None else None
            before_actor_id = str(current[1]) if current is not None else None
            if current is not None:
                self._disable_row(connection, before_binding_id or "", now)
            binding_id = self._identifier("BND")
            connection.execute(
                """
                INSERT INTO governance_external_actor_bindings(
                    binding_id, provider, provider_installation_ref, external_actor_id,
                    actor_id, binding_version, status, created_at, disabled_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, NULL)
                """,
                (
                    binding_id,
                    target.provider.value,
                    target.provider_installation_ref,
                    target.external_actor_id,
                    target.actor_ref.actor_id,
                    latest + 1,
                    now,
                ),
            )
            self._insert_transition(
                connection,
                transition_type=transition_type,
                provider=target.provider,
                installation=target.provider_installation_ref,
                external_actor_id=target.external_actor_id,
                before_binding_id=before_binding_id,
                after_binding_id=binding_id,
                before_actor_id=before_actor_id,
                after_actor_id=target.actor_ref.actor_id,
                approval=approval,
                occurred_at=now,
            )
            return self._binding_view(
                connection,
                binding_id,
                target.provider,
                target.provider_installation_ref,
                target.external_actor_id,
            )

    def _authorize_manager(
        self,
        connection: sqlite3.Connection,
        approval: BindingApproval,
    ) -> None:
        if approval.approved_by.actor_type is not ActorType.HUMAN:
            raise AuthorityResolutionError("AUTHORITY_DENIED", "승인자는 human이어야 합니다.")
        actor_type, _profile, status = self._actor_row(connection, approval.approved_by)
        if actor_type is not ActorType.HUMAN or status != ActorBindingStatus.ACTIVE.value:
            raise AuthorityResolutionError("AUTHORITY_DENIED", "승인자가 활성 human이 아닙니다.")
        permitted = connection.execute(
            """
            SELECT 1 FROM governance_actor_permissions
            WHERE actor_id = ? AND project_namespace = ? AND project_id = ?
              AND permission = 'authority.binding.manage'
            """,
            (
                approval.approved_by.actor_id,
                self.authority_project_ref.namespace,
                self.authority_project_ref.project_id,
            ),
        ).fetchone()
        if permitted is None:
            raise AuthorityResolutionError("AUTHORITY_DENIED", "binding manage 권한이 없습니다.")

    @staticmethod
    def _insert_actor(
        connection: sqlite3.Connection,
        actor_ref: ActorRef,
        profile: ActorProfile,
        now: str,
    ) -> None:
        try:
            connection.execute(
                """
                INSERT INTO governance_actors(
                    actor_id, actor_type, actor_profile, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'active', ?, ?)
                """,
                (actor_ref.actor_id, actor_ref.actor_type.value, profile.value, now, now),
            )
        except sqlite3.IntegrityError as error:
            raise AuthorityResolutionError(
                "ACTOR_ALREADY_EXISTS", "Actor가 이미 존재합니다."
            ) from error

    @staticmethod
    def _insert_permission(
        connection: sqlite3.Connection,
        actor_ref: ActorRef,
        project_ref: ProjectRef,
        permission: AuthorityPermission,
        granted_by: str,
        now: str,
    ) -> None:
        try:
            connection.execute(
                """
                INSERT INTO governance_actor_permissions(
                    actor_id, project_namespace, project_id, permission, granted_at, granted_by
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    actor_ref.actor_id,
                    project_ref.namespace,
                    project_ref.project_id,
                    permission.value,
                    now,
                    granted_by,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise AuthorityResolutionError(
                "PERMISSION_ALREADY_GRANTED", "permission이 이미 존재합니다."
            ) from error

    @staticmethod
    def _actor_row(
        connection: sqlite3.Connection,
        actor_ref: ActorRef,
    ) -> tuple[ActorType, ActorProfile, str]:
        row = connection.execute(
            """
            SELECT actor_type, actor_profile, status
            FROM governance_actors WHERE actor_id = ?
            """,
            (actor_ref.actor_id,),
        ).fetchone()
        if row is None or str(row[0]) != actor_ref.actor_type.value:
            raise AuthorityResolutionError("ACTOR_UNMAPPED", "Actor가 등록되지 않았습니다.")
        return ActorType(str(row[0])), ActorProfile(str(row[1])), str(row[2])

    @staticmethod
    def _active_binding(
        connection: sqlite3.Connection,
        provider: ChannelProvider,
        installation: str,
        external_actor_id: str,
    ) -> tuple[object, ...] | None:
        return cast(
            tuple[object, ...] | None,
            connection.execute(
                """
            SELECT binding_id, actor_id, binding_version
            FROM governance_external_actor_bindings
            WHERE provider = ? AND provider_installation_ref = ?
              AND external_actor_id = ? AND status = 'active'
            """,
                (provider.value, installation, external_actor_id),
            ).fetchone(),
        )

    @staticmethod
    def _latest_binding_version(
        connection: sqlite3.Connection,
        provider: ChannelProvider,
        installation: str,
        external_actor_id: str,
    ) -> int:
        row = connection.execute(
            """
            SELECT max(binding_version) FROM governance_external_actor_bindings
            WHERE provider = ? AND provider_installation_ref = ? AND external_actor_id = ?
            """,
            (provider.value, installation, external_actor_id),
        ).fetchone()
        return int(row[0]) if row is not None and row[0] is not None else 0

    @staticmethod
    def _disable_row(connection: sqlite3.Connection, binding_id: str, now: str) -> None:
        updated = connection.execute(
            """
            UPDATE governance_external_actor_bindings
            SET status = 'disabled', disabled_at = ?
            WHERE binding_id = ? AND status = 'active'
            """,
            (now, binding_id),
        )
        if updated.rowcount != 1:
            raise AuthorityResolutionError("BINDING_STALE", "active binding이 변경됐습니다.")

    @staticmethod
    def _insert_transition(
        connection: sqlite3.Connection,
        *,
        transition_type: str,
        provider: ChannelProvider,
        installation: str,
        external_actor_id: str,
        before_binding_id: str | None,
        after_binding_id: str | None,
        before_actor_id: str | None,
        after_actor_id: str | None,
        approval: BindingApproval,
        occurred_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO governance_binding_transitions(
                transition_id, transition_type, provider, provider_installation_ref,
                external_actor_id, before_binding_id, after_binding_id,
                before_actor_id, after_actor_id, approval_id, approved_by, reason, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ActorBindingService._identifier("BTR"),
                transition_type,
                provider.value,
                installation,
                external_actor_id,
                before_binding_id,
                after_binding_id,
                before_actor_id,
                after_actor_id,
                approval.approval_id,
                approval.approved_by.actor_id,
                approval.reason.strip(),
                occurred_at,
            ),
        )

    @staticmethod
    def _binding_view(
        connection: sqlite3.Connection,
        binding_id: str,
        provider: ChannelProvider,
        installation: str,
        external_actor_id: str,
    ) -> BindingView:
        row = connection.execute(
            """
            SELECT actor_id, binding_version, status, created_at, disabled_at
            FROM governance_external_actor_bindings WHERE binding_id = ?
            """,
            (binding_id,),
        ).fetchone()
        if row is None:
            raise AuthorityResolutionError("ACTOR_UNMAPPED", "binding이 없습니다.")
        actor_row = connection.execute(
            "SELECT actor_type FROM governance_actors WHERE actor_id = ?", (row[0],)
        ).fetchone()
        if actor_row is None:
            raise AuthorityResolutionError("ACTOR_UNMAPPED", "Actor가 없습니다.")
        return BindingView.model_validate(
            {
                "binding_id": binding_id,
                "target": {
                    "provider": provider,
                    "provider_installation_ref": installation,
                    "external_actor_id": external_actor_id,
                    "actor_ref": {"actor_id": str(row[0]), "actor_type": str(actor_row[0])},
                },
                "binding_version": int(row[1]),
                "status": str(row[2]),
                "created_at": str(row[3]),
                "disabled_at": row[4],
            }
        )

    @staticmethod
    def _identifier(prefix: str) -> str:
        return f"{prefix}-{secrets.token_hex(8).upper()}"

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp는 timezone-aware여야 합니다.")
        return value

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class AuthorityService:
    """The sole production constructor of server-created AuthorityContext."""

    def __init__(
        self,
        store: GovernanceStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self._clock = clock or _system_now

    def authenticate(
        self,
        request: DirectAuthorityRequest | IngressAuthorityRequest,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> AuthorityContext:
        authenticated_at = ActorBindingService._aware(self._clock())
        if isinstance(request, IngressAuthorityRequest):
            if connection is None or not connection.in_transaction:
                raise AuthorityResolutionError(
                    "GOVERNANCE_TRANSACTION_REQUIRED", "active transaction이 필요합니다."
                )
            return self._authenticate_ingress(connection, request, authenticated_at)
        _validate_channel_installation(
            request.provider,
            request.provider_installation_ref,
            request.channel,
        )
        if request.channel.provider is not request.provider:
            raise AuthorityResolutionError(
                "ACTION_CHANNEL_MISMATCH", "인증 provider context와 channel이 다릅니다."
            )
        if connection is not None:
            return self._authenticate_in_connection(
                connection,
                request.provider,
                request.provider_installation_ref,
                request.external_actor_id,
                request.project_ref,
                request.request_id,
                request.channel,
                authenticated_at,
            )
        with self.store.connect() as opened_connection:
            return self._authenticate_in_connection(
                opened_connection,
                request.provider,
                request.provider_installation_ref,
                request.external_actor_id,
                request.project_ref,
                request.request_id,
                request.channel,
                authenticated_at,
            )

    def authenticate_identity(self, identity: ExternalActorIdentity) -> ActorRef:
        """Verify an external identity before any project-independent durable write."""

        _validate_channel_installation(
            identity.provider,
            identity.provider_installation_ref,
            identity.channel,
        )
        if identity.channel.provider is not identity.provider:
            raise AuthorityResolutionError(
                "ACTION_CHANNEL_MISMATCH", "인증 provider context와 channel이 다릅니다."
            )
        with self.store.connect() as connection:
            binding = connection.execute(
                """
                SELECT b.actor_id, b.status, a.actor_type, a.status
                FROM governance_external_actor_bindings b
                JOIN governance_actors a ON a.actor_id = b.actor_id
                WHERE b.provider = ? AND b.provider_installation_ref = ?
                  AND b.external_actor_id = ?
                ORDER BY b.binding_version DESC LIMIT 1
                """,
                (
                    identity.provider.value,
                    identity.provider_installation_ref,
                    identity.external_actor_id,
                ),
            ).fetchone()
            has_submit_permission = False
            if binding is not None:
                has_submit_permission = (
                    connection.execute(
                        """
                        SELECT 1 FROM governance_actor_permissions
                        WHERE actor_id = ? AND permission = ?
                        LIMIT 1
                        """,
                        (
                            str(binding[0]),
                            AuthorityPermission.PROPOSAL_SUBMIT_REVIEW.value,
                        ),
                    ).fetchone()
                    is not None
                )
        if binding is None:
            raise AuthorityResolutionError("ACTOR_UNMAPPED", "매핑된 Actor가 없습니다.")
        if str(binding[1]) != ActorBindingStatus.ACTIVE.value or str(binding[3]) != "active":
            raise AuthorityResolutionError("ACTOR_DISABLED", "Actor binding이 비활성입니다.")
        if not has_submit_permission:
            raise AuthorityResolutionError(
                "PROJECT_ACCESS_DENIED", "Actor가 Intake submit permission을 갖지 않습니다."
            )
        return ActorRef.model_validate({"actor_id": str(binding[0]), "actor_type": str(binding[2])})

    def _authenticate_ingress(
        self,
        connection: sqlite3.Connection,
        request: IngressAuthorityRequest,
        authenticated_at: datetime,
    ) -> AuthorityContext:
        now = ActorBindingService._timestamp(ActorBindingService._aware(self._clock()))
        row = connection.execute(
            """
            SELECT i.provider, i.provider_installation_ref, i.external_actor_key,
                   i.channel_json, t.project_namespace, t.project_id
            FROM governance_ingress_commands i
            JOIN governance_action_tokens t
              ON t.token_id = i.credential_id
             AND t.token_hash = i.credential_hash
             AND t.allowed_action = i.action
            WHERE i.command_id = ? AND i.state = 'leased' AND i.lease_owner = ?
              AND i.claim_generation = ? AND i.lease_expires_at > ?
            """,
            (request.command_id, request.worker_id, request.generation, now),
        ).fetchone()
        if row is None:
            raise AuthorityResolutionError("INGRESS_LEASE_CONFLICT", "유효한 lease가 없습니다.")
        channel = ChannelRef.model_validate_json(str(row[3]))
        provider = ChannelProvider(str(row[0]))
        installation = str(row[1])
        _validate_channel_installation(provider, installation, channel)
        if channel.provider is not provider:
            raise AuthorityResolutionError(
                "ACTION_CHANNEL_MISMATCH", "인증 provider context와 channel이 다릅니다."
            )
        return self._authenticate_in_connection(
            connection,
            provider,
            installation,
            str(row[2]),
            ProjectRef(namespace=str(row[4]), project_id=str(row[5])),
            request.command_id,
            channel,
            authenticated_at,
        )

    @staticmethod
    def _authenticate_in_connection(
        connection: sqlite3.Connection,
        provider: ChannelProvider,
        installation: str,
        external_actor_id: str,
        project_ref: ProjectRef,
        request_id: str,
        channel: ChannelRef,
        authenticated_at: datetime,
    ) -> AuthorityContext:
        binding = connection.execute(
            """
            SELECT b.actor_id, b.status, a.actor_type, a.actor_profile, a.status
            FROM governance_external_actor_bindings b
            JOIN governance_actors a ON a.actor_id = b.actor_id
            WHERE b.provider = ? AND b.provider_installation_ref = ?
              AND b.external_actor_id = ?
            ORDER BY b.binding_version DESC LIMIT 1
            """,
            (provider.value, installation, external_actor_id),
        ).fetchone()
        if binding is None:
            raise AuthorityResolutionError("ACTOR_UNMAPPED", "매핑된 Actor가 없습니다.")
        if str(binding[1]) != ActorBindingStatus.ACTIVE.value or str(binding[4]) != "active":
            raise AuthorityResolutionError("ACTOR_DISABLED", "Actor binding이 비활성입니다.")
        actor_ref = ActorRef.model_validate(
            {"actor_id": str(binding[0]), "actor_type": str(binding[2])}
        )
        rows = connection.execute(
            """
            SELECT permission FROM governance_actor_permissions
            WHERE actor_id = ? AND project_namespace = ? AND project_id = ?
            ORDER BY permission
            """,
            (actor_ref.actor_id, project_ref.namespace, project_ref.project_id),
        ).fetchall()
        permissions = frozenset(AuthorityPermission(str(row[0])) for row in rows)
        if not permissions:
            raise AuthorityResolutionError(
                "PROJECT_ACCESS_DENIED", "Actor가 project permission을 갖지 않습니다."
            )
        if (
            actor_ref.actor_type is not ActorType.HUMAN
            and AuthorityPermission.PROPOSAL_DECIDE in permissions
        ):
            raise AuthorityResolutionError("AUTHORITY_DENIED", "human만 decision 가능합니다.")
        if str(binding[3]) == ActorProfile.INTAKE_POLICY.value and not permissions.issubset(
            ActorBindingService._INTAKE_PERMISSIONS
        ):
            raise AuthorityResolutionError("AUTHORITY_DENIED", "Intake 권한이 오염됐습니다.")
        return AuthorityContext(
            actor_ref=actor_ref,
            project_ref=project_ref,
            permissions=permissions,
            source=AuthoritySource(request_id=request_id, channel=channel),
            authenticated_at=authenticated_at,
        )
