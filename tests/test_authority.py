from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance import (
    ActorBindingService,
    ActorProfile,
    ActorRef,
    ActorType,
    AuthorityPermission,
    AuthorityResolutionError,
    AuthorityService,
    BindingApproval,
    BindingTarget,
    ChannelProvider,
    ChannelRef,
    DirectAuthorityRequest,
    IngressAuthorityRequest,
    VerifiedProviderCommand,
)
from amplai_foundry.governance.store import GovernanceStore, governance_transaction

NOW = datetime(2026, 7, 30, 11, 0, tzinfo=UTC)
AUTHORITY_PROJECT = ProjectRef(
    project_id="governance",
    namespace="org/default/project/governance",
)
PROJECT = ProjectRef(project_id="amplai", namespace="org/default/project/amplai")
OTHER_PROJECT = ProjectRef(project_id="cortex", namespace="org/default/project/cortex")
MANAGER = ActorRef(actor_id="ACT-MANAGER-1", actor_type=ActorType.HUMAN)
USER = ActorRef(actor_id="ACT-USER-1", actor_type=ActorType.HUMAN)
USER_TWO = ActorRef(actor_id="ACT-USER-2", actor_type=ActorType.HUMAN)
AGENT = ActorRef(actor_id="ACT-AGENT-1", actor_type=ActorType.AGENT)
INTAKE = ActorRef(actor_id="ACT-INTAKE-1", actor_type=ActorType.AGENT)
CHANNEL = ChannelRef(
    provider=ChannelProvider.SLACK,
    workspace_id="T123",
    channel_id="C456",
    message_id="1710000000.000200",
)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _approval(index: int, reason: str = "approved binding governance change") -> BindingApproval:
    return BindingApproval(
        approval_id=f"APR-{index:016X}",
        approved_by=MANAGER,
        reason=reason,
    )


def _target(actor: ActorRef = USER) -> BindingTarget:
    return BindingTarget(
        provider=ChannelProvider.SLACK,
        provider_installation_ref="T123:APP1",
        external_actor_id="U456",
        actor_ref=actor,
    )


def _setup(
    tmp_path: Path,
) -> tuple[GovernanceStore, MutableClock, ActorBindingService, AuthorityService]:
    store = GovernanceStore(tmp_path / "governance.db")
    store.initialize()
    clock = MutableClock(NOW)
    bindings = ActorBindingService(store, AUTHORITY_PROJECT, clock=clock)
    bindings.bootstrap_manager(MANAGER)
    bindings.register_actor(USER, approval=_approval(1))
    bindings.register_actor(USER_TWO, approval=_approval(2))
    bindings.register_actor(AGENT, approval=_approval(3))
    bindings.register_actor(INTAKE, profile=ActorProfile.INTAKE_POLICY, approval=_approval(4))
    bindings.grant_permission(
        USER,
        PROJECT,
        AuthorityPermission.PROPOSAL_READ,
        approval=_approval(5),
    )
    bindings.grant_permission(
        USER,
        PROJECT,
        AuthorityPermission.PROPOSAL_DECIDE,
        approval=_approval(6),
    )
    bindings.grant_permission(
        USER_TWO,
        PROJECT,
        AuthorityPermission.PROPOSAL_DECIDE,
        approval=_approval(7),
    )
    bindings.grant_permission(
        INTAKE,
        PROJECT,
        AuthorityPermission.PROPOSAL_READ,
        approval=_approval(8),
    )
    bindings.grant_permission(
        INTAKE,
        PROJECT,
        AuthorityPermission.PROPOSAL_SUBMIT_REVIEW,
        approval=_approval(9),
    )
    return store, clock, bindings, AuthorityService(store, clock=clock)


def _authenticate(service: AuthorityService, project_ref: ProjectRef = PROJECT):
    return service.authenticate(
        DirectAuthorityRequest(
            provider=ChannelProvider.SLACK,
            provider_installation_ref="T123:APP1",
            external_actor_id="U456",
            project_ref=project_ref,
            request_id="CMD-0123456789ABCDEF",
            channel=CHANNEL,
        )
    )


def test_authority_is_server_created_from_active_binding_and_project_permission(
    tmp_path: Path,
) -> None:
    _store, _clock, bindings, authority_service = _setup(tmp_path)
    bindings.create_binding(_target(), approval=_approval(10))

    authority = _authenticate(authority_service)

    assert authority.actor_ref == USER
    assert authority.project_ref == PROJECT
    assert authority.permissions == frozenset(
        {AuthorityPermission.PROPOSAL_READ, AuthorityPermission.PROPOSAL_DECIDE}
    )
    assert authority.source.request_id == "CMD-0123456789ABCDEF"
    assert authority.authenticated_at == NOW


def test_slack_installation_workspace_must_match_authority_channel(tmp_path: Path) -> None:
    _store, _clock, bindings, authority_service = _setup(tmp_path)
    bindings.create_binding(_target(), approval=_approval(10))
    mismatched = DirectAuthorityRequest(
        provider=ChannelProvider.SLACK,
        provider_installation_ref="T123:APP1",
        external_actor_id="U456",
        project_ref=PROJECT,
        request_id="CMD-0123456789ABCDEF",
        channel=CHANNEL.model_copy(update={"workspace_id": "T999"}),
    )

    with pytest.raises(AuthorityResolutionError, match="ACTION_CHANNEL_MISMATCH"):
        authority_service.authenticate(mismatched)


def test_binding_to_disabled_actor_is_rejected_without_transition(tmp_path: Path) -> None:
    _store, _clock, bindings, _authority = _setup(tmp_path)
    bindings.disable_actor(USER, approval=_approval(10))

    with pytest.raises(AuthorityResolutionError, match="ACTOR_DISABLED"):
        bindings.create_binding(_target(), approval=_approval(11))

    assert bindings.list_transitions() == ()


@pytest.mark.parametrize("injected_field", ("permissions", "authority_context"))
def test_public_ingress_command_rejects_authority_injection(injected_field: str) -> None:
    payload: dict[str, object] = {
        "external_event_id": "EVT-1",
        "external_actor_key": "U456",
        "channel_ref": CHANNEL,
        "credential_id": "TOK-0123456789ABCDEF",
        "raw_credential": "opaque-secret",
        "action": "approve",
        injected_field: [],
    }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        VerifiedProviderCommand.model_validate(payload)


def test_create_rebind_disable_records_approved_append_only_diff(tmp_path: Path) -> None:
    store, clock, bindings, authority_service = _setup(tmp_path)
    created = bindings.create_binding(_target(), approval=_approval(10, "create external binding"))
    clock.value += timedelta(seconds=1)
    rebound = bindings.rebind(
        _target(USER_TWO),
        approval=_approval(11, "move identity to replacement actor"),
    )

    assert created.status.value == "active"
    assert rebound.binding_version == 2
    assert rebound.target.actor_ref == USER_TWO
    assert _authenticate(authority_service).actor_ref == USER_TWO
    clock.value += timedelta(seconds=1)
    disabled = bindings.disable_binding(
        provider=ChannelProvider.SLACK,
        provider_installation_ref="T123:APP1",
        external_actor_id="U456",
        approval=_approval(12, "disable retired external identity"),
    )
    assert disabled.status.value == "disabled"
    with pytest.raises(AuthorityResolutionError, match="ACTOR_DISABLED"):
        _authenticate(authority_service)

    transitions = bindings.list_transitions()
    assert [item.transition_type for item in transitions] == ["create", "rebind", "disable"]
    assert transitions[1].before_actor_id == USER.actor_id
    assert transitions[1].after_actor_id == USER_TWO.actor_id
    assert transitions[1].reason == "move identity to replacement actor"
    with store.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE governance_binding_transitions SET reason = 'tamper'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM governance_binding_transitions")


def test_binding_silent_overwrite_and_approval_reuse_are_rejected(tmp_path: Path) -> None:
    _store, _clock, bindings, _authority = _setup(tmp_path)
    approval = _approval(10)
    bindings.create_binding(_target(), approval=approval)

    with pytest.raises(AuthorityResolutionError, match="BINDING_ALREADY_EXISTS"):
        bindings.create_binding(_target(USER_TWO), approval=_approval(11))
    with pytest.raises(sqlite3.IntegrityError):
        bindings.rebind(_target(USER_TWO), approval=approval)


def test_concurrent_binding_create_has_one_winner(tmp_path: Path) -> None:
    _store, _clock, bindings, _authority = _setup(tmp_path)

    def create(pair: tuple[ActorRef, int]) -> str:
        actor, approval_index = pair
        try:
            bindings.create_binding(_target(actor), approval=_approval(approval_index))
        except (AuthorityResolutionError, sqlite3.IntegrityError):
            return "rejected"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(create, ((USER, 10), (USER_TWO, 11))))

    assert sorted(outcomes) == ["created", "rejected"]
    assert len(bindings.list_transitions()) == 1


def test_rebind_transition_failure_rolls_back_old_binding_and_new_binding(tmp_path: Path) -> None:
    store, _clock, bindings, authority_service = _setup(tmp_path)
    original = bindings.create_binding(_target(), approval=_approval(10))
    with store.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_rebind_transition
            BEFORE INSERT ON governance_binding_transitions
            WHEN NEW.transition_type = 'rebind'
            BEGIN
                SELECT RAISE(ABORT, 'injected rebind failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected rebind failure"):
        bindings.rebind(_target(USER_TWO), approval=_approval(11))

    assert _authenticate(authority_service).actor_ref == USER
    with store.connect() as connection:
        rows = connection.execute(
            """
            SELECT binding_id, actor_id, status
            FROM governance_external_actor_bindings ORDER BY binding_version
            """
        ).fetchall()
    assert rows == [(original.binding_id, USER.actor_id, "active")]


def test_unmapped_disabled_and_cross_project_authority_fail_closed(tmp_path: Path) -> None:
    _store, _clock, bindings, authority_service = _setup(tmp_path)
    with pytest.raises(AuthorityResolutionError, match="ACTOR_UNMAPPED"):
        _authenticate(authority_service)

    bindings.create_binding(_target(), approval=_approval(10))
    with pytest.raises(AuthorityResolutionError, match="PROJECT_ACCESS_DENIED"):
        _authenticate(authority_service, OTHER_PROJECT)

    bindings.disable_actor(USER, approval=_approval(11))
    with pytest.raises(AuthorityResolutionError, match="ACTOR_DISABLED"):
        _authenticate(authority_service)


def test_human_decision_and_intake_policy_permissions_are_enforced(tmp_path: Path) -> None:
    _store, _clock, bindings, _authority = _setup(tmp_path)
    with pytest.raises(AuthorityResolutionError, match="human만 decision"):
        bindings.grant_permission(
            AGENT,
            PROJECT,
            AuthorityPermission.PROPOSAL_DECIDE,
            approval=_approval(10),
        )
    with pytest.raises(AuthorityResolutionError, match="Intake Policy Actor"):
        bindings.grant_permission(
            INTAKE,
            PROJECT,
            AuthorityPermission.PROPOSAL_REQUEST_APPLY,
            approval=_approval(11),
        )

    bindings.create_binding(
        BindingTarget(
            provider=ChannelProvider.SLACK,
            provider_installation_ref="T123:APP1",
            external_actor_id="UINTAKE",
            actor_ref=INTAKE,
        ),
        approval=_approval(12),
    )
    authority = AuthorityService(bindings.store, clock=lambda: NOW).authenticate(
        DirectAuthorityRequest(
            provider=ChannelProvider.SLACK,
            provider_installation_ref="T123:APP1",
            external_actor_id="UINTAKE",
            project_ref=PROJECT,
            request_id="CMD-1111111111111111",
            channel=CHANNEL,
        )
    )
    assert authority.permissions == frozenset(
        {
            AuthorityPermission.PROPOSAL_READ,
            AuthorityPermission.PROPOSAL_SUBMIT_REVIEW,
        }
    )


def _seed_leased_ingress(store: GovernanceStore) -> str:
    command_id = "CMD-0123456789ABCDEF"
    channel_json = json.dumps(
        CHANNEL.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    body_digest = f"sha256:{hashlib.sha256(b'body').hexdigest()}"
    credential_hash = f"sha256:{hashlib.sha256(b'token').hexdigest()}"
    with store.connect() as connection:
        connection.execute(
            """
            INSERT INTO governance_active_proposals(
                project_namespace, project_id, proposal_id,
                active_definition_digest, content_revision, state_revision,
                decision_epoch, status, created_at, updated_at
            ) VALUES (?, ?, 'PROP-20260730-ABCDEF12', ?, 1, 2, 1,
                      'reviewed', ?, ?)
            """,
            (
                PROJECT.namespace,
                PROJECT.project_id,
                f"sha256:{'1' * 64}",
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO governance_action_tokens(
                token_id, token_hash, project_namespace, project_id, proposal_id,
                active_definition_digest, content_revision, state_revision,
                decision_epoch, allowed_action, allowed_actor_id,
                allowed_actor_type, bound_channel_json, issued_at, expires_at,
                state, resolved_at
            ) VALUES ('TOK-0123456789ABCDEF', ?, ?, ?,
                      'PROP-20260730-ABCDEF12', ?, 1, 2, 1, 'approve', ?,
                      'human', ?, ?, ?, 'issued', NULL)
            """,
            (
                credential_hash,
                PROJECT.namespace,
                PROJECT.project_id,
                f"sha256:{'1' * 64}",
                USER.actor_id,
                channel_json,
                NOW.isoformat(),
                (NOW + timedelta(minutes=15)).isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO governance_ingress_commands(
                command_id, provider, provider_installation_ref, provider_fingerprint,
                raw_body_digest, external_event_id, external_actor_key, channel_json,
                credential_kind, credential_id, credential_hash, action, received_at,
                state, attempts, claim_generation, lease_owner, lease_expires_at,
                retry_at, completed_at, last_error_code
            ) VALUES (?, 'slack', 'T123:APP1', ?, ?, 'EVT-1', 'U456', ?,
                      'action_token', 'TOK-0123456789ABCDEF', ?, 'approve', ?,
                      'leased', 1, 1, 'worker', ?, NULL, NULL, NULL)
            """,
            (
                command_id,
                hashlib.sha256(b"fingerprint").hexdigest(),
                body_digest,
                channel_json,
                credential_hash,
                NOW.isoformat(),
                (NOW + timedelta(minutes=1)).isoformat(),
            ),
        )
    return command_id


def test_ingress_authority_is_reevaluated_in_worker_transaction(tmp_path: Path) -> None:
    store, _clock, bindings, authority_service = _setup(tmp_path)
    bindings.create_binding(_target(), approval=_approval(10))
    command_id = _seed_leased_ingress(store)
    bindings.rebind(_target(USER_TWO), approval=_approval(11))

    with store.connect() as connection, governance_transaction(connection):
        authority = authority_service.authenticate(
            IngressAuthorityRequest(
                command_id=command_id,
                worker_id="worker",
                generation=1,
            ),
            connection=connection,
        )

    assert authority.actor_ref == USER_TWO
    assert authority.project_ref == PROJECT
    assert authority.permissions == frozenset({AuthorityPermission.PROPOSAL_DECIDE})


def test_ingress_authority_request_rejects_caller_selected_project() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        IngressAuthorityRequest.model_validate(
            {
                "command_id": "CMD-0123456789ABCDEF",
                "worker_id": "worker",
                "generation": 1,
                "project_ref": OTHER_PROJECT,
            }
        )


def test_authority_failure_does_not_mutate_ingress_or_governance_aggregates(tmp_path: Path) -> None:
    store, _clock, _bindings, authority_service = _setup(tmp_path)
    command_id = _seed_leased_ingress(store)
    with store.connect() as connection:
        before = connection.execute(
            "SELECT state, attempts, claim_generation FROM governance_ingress_commands"
        ).fetchall()
        token_count = connection.execute("SELECT count(*) FROM governance_action_tokens").fetchone()
        result_count = connection.execute(
            "SELECT count(*) FROM governance_decision_results"
        ).fetchone()

    with (
        store.connect() as connection,
        governance_transaction(connection),
        pytest.raises(AuthorityResolutionError, match="ACTOR_UNMAPPED"),
    ):
        authority_service.authenticate(
            IngressAuthorityRequest(
                command_id=command_id,
                worker_id="worker",
                generation=1,
            ),
            connection=connection,
        )

    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT state, attempts, claim_generation FROM governance_ingress_commands"
            ).fetchall()
            == before
        )
        assert (
            connection.execute("SELECT count(*) FROM governance_action_tokens").fetchone()
            == token_count
        )
        assert (
            connection.execute("SELECT count(*) FROM governance_decision_results").fetchone()
            == result_count
        )
