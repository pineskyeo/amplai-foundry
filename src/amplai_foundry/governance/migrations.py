"""Versioned SQLite migrations for mutable governance state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import cast


class GovernanceMigrationError(RuntimeError):
    """The Governance Store schema cannot be verified or migrated safely."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        payload = f"{self.version}\n{self.name}\n" + "\n-- statement --\n".join(self.statements)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


INITIAL_MIGRATIONS = (
    Migration(
        version=1,
        name="governance-store-foundation",
        statements=(
            """
            CREATE TABLE governance_store_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID
            """,
            """
            INSERT INTO governance_store_metadata(key, value)
            VALUES ('store_kind', 'amplai-governance')
            """,
        ),
    ),
    Migration(
        version=2,
        name="active-proposal-cas",
        statements=(
            """
            CREATE TABLE governance_active_proposals (
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                active_definition_digest TEXT NOT NULL,
                content_revision INTEGER NOT NULL CHECK (content_revision >= 1),
                state_revision INTEGER NOT NULL CHECK (state_revision >= 1),
                decision_epoch INTEGER NOT NULL CHECK (decision_epoch >= 1),
                status TEXT NOT NULL CHECK (
                    status IN (
                        'draft',
                        'reviewed',
                        'changes_requested',
                        'approved',
                        'apply_requested',
                        'apply_failed',
                        'applied',
                        'rejected',
                        'superseded'
                    )
                ),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (project_namespace, project_id, proposal_id),
                CHECK (
                    length(active_definition_digest) = 71
                    AND substr(active_definition_digest, 1, 7) = 'sha256:'
                    AND substr(active_definition_digest, 8) NOT GLOB '*[^0-9a-f]*'
                )
            ) WITHOUT ROWID
            """,
            """
            CREATE TABLE governance_definition_revisions (
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                content_revision INTEGER NOT NULL CHECK (content_revision >= 1),
                definition_digest TEXT NOT NULL,
                previous_definition_digest TEXT,
                activated_from_status TEXT,
                activated_at TEXT NOT NULL,
                PRIMARY KEY (
                    project_namespace,
                    project_id,
                    proposal_id,
                    content_revision
                ),
                UNIQUE (
                    project_namespace,
                    project_id,
                    proposal_id,
                    definition_digest
                ),
                FOREIGN KEY (project_namespace, project_id, proposal_id)
                    REFERENCES governance_active_proposals(
                        project_namespace,
                        project_id,
                        proposal_id
                    )
                    ON DELETE RESTRICT,
                CHECK (
                    length(definition_digest) = 71
                    AND substr(definition_digest, 1, 7) = 'sha256:'
                    AND substr(definition_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    previous_definition_digest IS NULL
                    OR (
                        length(previous_definition_digest) = 71
                        AND substr(previous_definition_digest, 1, 7) = 'sha256:'
                        AND substr(previous_definition_digest, 8) NOT GLOB '*[^0-9a-f]*'
                    )
                ),
                CHECK (
                    activated_from_status IS NULL
                    OR activated_from_status IN ('draft', 'changes_requested')
                ),
                CHECK (
                    (content_revision = 1 AND previous_definition_digest IS NULL)
                    OR
                    (content_revision > 1 AND previous_definition_digest IS NOT NULL)
                )
            ) WITHOUT ROWID
            """,
        ),
    ),
    Migration(
        version=3,
        name="decision-token-replay",
        statements=(
            """
            CREATE TABLE governance_action_tokens (
                token_id TEXT PRIMARY KEY NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                active_definition_digest TEXT NOT NULL,
                content_revision INTEGER NOT NULL CHECK (content_revision >= 1),
                state_revision INTEGER NOT NULL CHECK (state_revision >= 1),
                decision_epoch INTEGER NOT NULL CHECK (decision_epoch >= 1),
                allowed_action TEXT NOT NULL CHECK (
                    allowed_action IN ('approve', 'reject', 'request_changes')
                ),
                allowed_actor_id TEXT NOT NULL,
                allowed_actor_type TEXT NOT NULL CHECK (allowed_actor_type = 'human'),
                bound_channel_json TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('issued', 'consumed', 'expired', 'revoked')),
                resolved_at TEXT,
                FOREIGN KEY (project_namespace, project_id, proposal_id)
                    REFERENCES governance_active_proposals(
                        project_namespace,
                        project_id,
                        proposal_id
                    )
                    ON DELETE RESTRICT,
                CHECK (
                    length(token_id) = 20
                    AND substr(token_id, 1, 4) = 'TOK-'
                    AND substr(token_id, 5) NOT GLOB '*[^A-F0-9]*'
                ),
                CHECK (
                    length(token_hash) = 71
                    AND substr(token_hash, 1, 7) = 'sha256:'
                    AND substr(token_hash, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(active_definition_digest) = 71
                    AND substr(active_definition_digest, 1, 7) = 'sha256:'
                    AND substr(active_definition_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    (state = 'issued' AND resolved_at IS NULL)
                    OR (state != 'issued' AND resolved_at IS NOT NULL)
                )
            )
            """,
            """
            CREATE TABLE governance_decision_results (
                idempotency_key TEXT PRIMARY KEY,
                request_fingerprint TEXT NOT NULL,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                action TEXT NOT NULL CHECK (
                    action IN ('approve', 'reject', 'request_changes')
                ),
                actor_id TEXT NOT NULL,
                actor_type TEXT NOT NULL CHECK (actor_type = 'human'),
                channel_json TEXT NOT NULL,
                proposal_status TEXT NOT NULL CHECK (
                    proposal_status IN ('approved', 'rejected', 'changes_requested')
                ),
                active_definition_digest TEXT NOT NULL,
                content_revision INTEGER NOT NULL CHECK (content_revision >= 1),
                state_revision INTEGER NOT NULL CHECK (state_revision >= 2),
                decision_epoch INTEGER NOT NULL CHECK (decision_epoch >= 1),
                token_id TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                FOREIGN KEY (project_namespace, project_id, proposal_id)
                    REFERENCES governance_active_proposals(
                        project_namespace,
                        project_id,
                        proposal_id
                    )
                    ON DELETE RESTRICT,
                FOREIGN KEY (token_id)
                    REFERENCES governance_action_tokens(token_id)
                    ON DELETE RESTRICT,
                CHECK (
                    length(request_fingerprint) = 64
                    AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(active_definition_digest) = 71
                    AND substr(active_definition_digest, 1, 7) = 'sha256:'
                    AND substr(active_definition_digest, 8) NOT GLOB '*[^0-9a-f]*'
                )
            ) WITHOUT ROWID
            """,
        ),
    ),
    Migration(
        version=4,
        name="durable-provider-ingress",
        statements=(
            """
            CREATE TABLE governance_ingress_commands (
                command_id TEXT PRIMARY KEY NOT NULL,
                provider TEXT NOT NULL CHECK (
                    provider IN ('slack', 'telegram', 'hermes', 'web', 'cli')
                ),
                provider_installation_ref TEXT NOT NULL,
                provider_fingerprint TEXT NOT NULL,
                raw_body_digest TEXT NOT NULL,
                external_event_id TEXT NOT NULL,
                external_actor_key TEXT NOT NULL,
                channel_json TEXT NOT NULL,
                credential_kind TEXT NOT NULL CHECK (credential_kind = 'action_token'),
                credential_id TEXT NOT NULL,
                credential_hash TEXT NOT NULL,
                action TEXT NOT NULL CHECK (
                    action IN ('approve', 'reject', 'request_changes')
                ),
                received_at TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN (
                        'pending', 'leased', 'completed', 'retry_wait',
                        'recovery_hold', 'dead_letter'
                    )
                ),
                attempts INTEGER NOT NULL CHECK (attempts >= 0),
                claim_generation INTEGER NOT NULL CHECK (claim_generation >= 0),
                lease_owner TEXT,
                lease_expires_at TEXT,
                retry_at TEXT,
                completed_at TEXT,
                last_error_code TEXT,
                UNIQUE (provider, provider_installation_ref, provider_fingerprint),
                UNIQUE (provider, provider_installation_ref, external_event_id),
                CHECK (
                    length(command_id) = 20
                    AND substr(command_id, 1, 4) = 'CMD-'
                    AND substr(command_id, 5) NOT GLOB '*[^A-F0-9]*'
                ),
                CHECK (
                    length(provider_fingerprint) = 64
                    AND provider_fingerprint NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(raw_body_digest) = 71
                    AND substr(raw_body_digest, 1, 7) = 'sha256:'
                    AND substr(raw_body_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(credential_hash) = 71
                    AND substr(credential_hash, 1, 7) = 'sha256:'
                    AND substr(credential_hash, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    (state = 'leased' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
                    OR (state != 'leased' AND lease_owner IS NULL AND lease_expires_at IS NULL)
                ),
                CHECK (
                    (state = 'retry_wait' AND retry_at IS NOT NULL)
                    OR (state != 'retry_wait' AND retry_at IS NULL)
                ),
                CHECK (
                    (state = 'completed' AND completed_at IS NOT NULL)
                    OR (state != 'completed' AND completed_at IS NULL)
                )
            )
            """,
        ),
    ),
    Migration(
        version=5,
        name="authority-actor-binding",
        statements=(
            """
            CREATE TABLE governance_actors (
                actor_id TEXT PRIMARY KEY NOT NULL,
                actor_type TEXT NOT NULL CHECK (actor_type IN ('human', 'service', 'agent')),
                actor_profile TEXT NOT NULL CHECK (
                    actor_profile IN ('standard', 'intake_policy')
                ),
                status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (
                    substr(actor_id, 1, 4) = 'ACT-'
                    AND length(actor_id) > 4
                    AND substr(actor_id, 5) NOT GLOB '*[^A-Z0-9-]*'
                )
            )
            """,
            """
            CREATE TABLE governance_external_actor_bindings (
                binding_id TEXT PRIMARY KEY NOT NULL,
                provider TEXT NOT NULL CHECK (
                    provider IN ('slack', 'telegram', 'hermes', 'web', 'cli')
                ),
                provider_installation_ref TEXT NOT NULL,
                external_actor_id TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
                status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
                created_at TEXT NOT NULL,
                disabled_at TEXT,
                UNIQUE (
                    provider, provider_installation_ref, external_actor_id, binding_version
                ),
                FOREIGN KEY (actor_id) REFERENCES governance_actors(actor_id) ON DELETE RESTRICT,
                CHECK (
                    (status = 'active' AND disabled_at IS NULL)
                    OR (status = 'disabled' AND disabled_at IS NOT NULL)
                )
            )
            """,
            """
            CREATE UNIQUE INDEX governance_one_active_external_binding
            ON governance_external_actor_bindings(
                provider, provider_installation_ref, external_actor_id
            )
            WHERE status = 'active'
            """,
            """
            CREATE TABLE governance_actor_permissions (
                actor_id TEXT NOT NULL,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                permission TEXT NOT NULL CHECK (
                    permission IN (
                        'proposal.read', 'proposal.submit_review', 'proposal.decide',
                        'proposal.request_apply', 'proposal.apply.execute',
                        'authority.binding.manage', 'activation.manage'
                    )
                ),
                granted_at TEXT NOT NULL,
                granted_by TEXT NOT NULL,
                PRIMARY KEY (actor_id, project_namespace, project_id, permission),
                FOREIGN KEY (actor_id) REFERENCES governance_actors(actor_id) ON DELETE RESTRICT,
                FOREIGN KEY (granted_by) REFERENCES governance_actors(actor_id) ON DELETE RESTRICT
            ) WITHOUT ROWID
            """,
            """
            CREATE TABLE governance_binding_transitions (
                transition_id TEXT PRIMARY KEY NOT NULL,
                transition_type TEXT NOT NULL CHECK (
                    transition_type IN ('create', 'rebind', 'disable')
                ),
                provider TEXT NOT NULL,
                provider_installation_ref TEXT NOT NULL,
                external_actor_id TEXT NOT NULL,
                before_binding_id TEXT,
                after_binding_id TEXT,
                before_actor_id TEXT,
                after_actor_id TEXT,
                approval_id TEXT NOT NULL UNIQUE,
                approved_by TEXT NOT NULL,
                reason TEXT NOT NULL CHECK (length(trim(reason)) >= 3),
                occurred_at TEXT NOT NULL,
                FOREIGN KEY (before_binding_id)
                    REFERENCES governance_external_actor_bindings(binding_id) ON DELETE RESTRICT,
                FOREIGN KEY (after_binding_id)
                    REFERENCES governance_external_actor_bindings(binding_id) ON DELETE RESTRICT,
                FOREIGN KEY (before_actor_id)
                    REFERENCES governance_actors(actor_id) ON DELETE RESTRICT,
                FOREIGN KEY (after_actor_id)
                    REFERENCES governance_actors(actor_id) ON DELETE RESTRICT,
                FOREIGN KEY (approved_by)
                    REFERENCES governance_actors(actor_id) ON DELETE RESTRICT,
                CHECK (
                    (transition_type = 'create' AND before_binding_id IS NULL
                        AND after_binding_id IS NOT NULL AND before_actor_id IS NULL
                        AND after_actor_id IS NOT NULL)
                    OR (transition_type = 'rebind' AND before_binding_id IS NOT NULL
                        AND after_binding_id IS NOT NULL AND before_actor_id IS NOT NULL
                        AND after_actor_id IS NOT NULL)
                    OR (transition_type = 'disable' AND before_binding_id IS NOT NULL
                        AND after_binding_id IS NULL AND before_actor_id IS NOT NULL
                        AND after_actor_id IS NULL)
                )
            )
            """,
            """
            CREATE TRIGGER governance_binding_transitions_no_update
            BEFORE UPDATE ON governance_binding_transitions
            BEGIN
                SELECT RAISE(ABORT, 'binding transitions are append-only');
            END
            """,
            """
            CREATE TRIGGER governance_binding_transitions_no_delete
            BEFORE DELETE ON governance_binding_transitions
            BEGIN
                SELECT RAISE(ABORT, 'binding transitions are append-only');
            END
            """,
        ),
    ),
    Migration(
        version=6,
        name="ordered-transactional-outbox",
        statements=(
            """
            CREATE TABLE governance_aggregate_sequences (
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                aggregate_sequence INTEGER NOT NULL CHECK (aggregate_sequence >= 0),
                last_event_hash TEXT,
                PRIMARY KEY (project_namespace, project_id, proposal_id),
                FOREIGN KEY (project_namespace, project_id, proposal_id)
                    REFERENCES governance_active_proposals(
                        project_namespace, project_id, proposal_id
                    ) ON DELETE RESTRICT,
                CHECK (
                    last_event_hash IS NULL OR (
                        length(last_event_hash) = 71
                        AND substr(last_event_hash, 1, 7) = 'sha256:'
                        AND substr(last_event_hash, 8) NOT GLOB '*[^0-9a-f]*'
                    )
                )
            ) WITHOUT ROWID
            """,
            """
            CREATE TABLE governance_audit_events (
                event_id TEXT PRIMARY KEY NOT NULL,
                command_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                aggregate_sequence INTEGER NOT NULL CHECK (aggregate_sequence >= 1),
                actor_id TEXT NOT NULL,
                actor_type TEXT NOT NULL CHECK (actor_type IN ('human', 'service', 'agent')),
                policy_snapshot_id TEXT NOT NULL,
                before_state TEXT NOT NULL,
                after_state TEXT NOT NULL,
                definition_digest TEXT NOT NULL,
                previous_event_hash TEXT,
                event_hash TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                UNIQUE (project_namespace, project_id, proposal_id, aggregate_sequence),
                FOREIGN KEY (project_namespace, project_id, proposal_id)
                    REFERENCES governance_active_proposals(
                        project_namespace, project_id, proposal_id
                    ) ON DELETE RESTRICT,
                CHECK (
                    length(definition_digest) = 71
                    AND substr(definition_digest, 1, 7) = 'sha256:'
                    AND substr(definition_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    previous_event_hash IS NULL OR (
                        length(previous_event_hash) = 71
                        AND substr(previous_event_hash, 1, 7) = 'sha256:'
                        AND substr(previous_event_hash, 8) NOT GLOB '*[^0-9a-f]*'
                    )
                ),
                CHECK (
                    length(event_hash) = 71
                    AND substr(event_hash, 1, 7) = 'sha256:'
                    AND substr(event_hash, 8) NOT GLOB '*[^0-9a-f]*'
                )
            )
            """,
            """
            CREATE TRIGGER governance_audit_events_no_update
            BEFORE UPDATE ON governance_audit_events
            BEGIN
                SELECT RAISE(ABORT, 'governance audit is append-only');
            END
            """,
            """
            CREATE TRIGGER governance_audit_events_no_delete
            BEFORE DELETE ON governance_audit_events
            BEGIN
                SELECT RAISE(ABORT, 'governance audit is append-only');
            END
            """,
            """
            CREATE TABLE governance_outbox_destinations (
                destination_ref TEXT PRIMARY KEY NOT NULL,
                next_sequence INTEGER NOT NULL CHECK (next_sequence >= 1),
                delivered_sequence INTEGER NOT NULL CHECK (delivered_sequence >= 0),
                operator_hold INTEGER NOT NULL CHECK (operator_hold IN (0, 1)),
                updated_at TEXT NOT NULL,
                CHECK (delivered_sequence < next_sequence)
            ) WITHOUT ROWID
            """,
            """
            CREATE TABLE governance_outbox_events (
                event_id TEXT PRIMARY KEY NOT NULL,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                aggregate_sequence INTEGER NOT NULL CHECK (aggregate_sequence >= 1),
                destination_ref TEXT NOT NULL,
                destination_sequence INTEGER NOT NULL CHECK (destination_sequence >= 1),
                source_state_revision INTEGER NOT NULL CHECK (source_state_revision >= 1),
                supersession_key TEXT,
                payload_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN (
                        'pending', 'leased', 'retry_wait', 'delivered',
                        'superseded', 'dead_letter', 'recovery_hold'
                    )
                ),
                attempts INTEGER NOT NULL CHECK (attempts >= 0),
                claim_generation INTEGER NOT NULL CHECK (claim_generation >= 0),
                lease_owner TEXT,
                lease_expires_at TEXT,
                retry_at TEXT,
                delivered_at TEXT,
                remote_receipt TEXT,
                last_error_code TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (destination_ref, destination_sequence),
                UNIQUE (
                    project_namespace, project_id, proposal_id,
                    aggregate_sequence, destination_ref
                ),
                FOREIGN KEY (project_namespace, project_id, proposal_id, aggregate_sequence)
                    REFERENCES governance_audit_events(
                        project_namespace, project_id, proposal_id, aggregate_sequence
                    ) ON DELETE RESTRICT,
                FOREIGN KEY (destination_ref)
                    REFERENCES governance_outbox_destinations(destination_ref) ON DELETE RESTRICT,
                CHECK (
                    length(payload_digest) = 71
                    AND substr(payload_digest, 1, 7) = 'sha256:'
                    AND substr(payload_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    (state = 'leased' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
                    OR (state != 'leased' AND lease_owner IS NULL AND lease_expires_at IS NULL)
                ),
                CHECK (
                    (state = 'retry_wait' AND retry_at IS NOT NULL)
                    OR (state != 'retry_wait' AND retry_at IS NULL)
                ),
                CHECK (
                    (state = 'delivered' AND delivered_at IS NOT NULL)
                    OR (state != 'delivered' AND delivered_at IS NULL)
                )
            )
            """,
            """
            CREATE TRIGGER governance_outbox_payload_immutable
            BEFORE UPDATE ON governance_outbox_events
            WHEN OLD.project_namespace != NEW.project_namespace
              OR OLD.project_id != NEW.project_id
              OR OLD.proposal_id != NEW.proposal_id
              OR OLD.aggregate_sequence != NEW.aggregate_sequence
              OR OLD.destination_ref != NEW.destination_ref
              OR OLD.destination_sequence != NEW.destination_sequence
              OR OLD.source_state_revision != NEW.source_state_revision
              OR OLD.supersession_key IS NOT NEW.supersession_key
              OR OLD.payload_digest != NEW.payload_digest
              OR OLD.payload_json != NEW.payload_json
              OR OLD.created_at != NEW.created_at
            BEGIN
                SELECT RAISE(ABORT, 'outbox payload is immutable');
            END
            """,
            """
            CREATE TRIGGER governance_outbox_events_no_delete
            BEFORE DELETE ON governance_outbox_events
            BEGIN
                SELECT RAISE(ABORT, 'governance outbox is durable');
            END
            """,
            """
            CREATE TABLE governance_outbox_dead_letters (
                dead_letter_id TEXT PRIMARY KEY NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                destination_ref TEXT NOT NULL,
                destination_sequence INTEGER NOT NULL CHECK (destination_sequence >= 1),
                attempts INTEGER NOT NULL CHECK (attempts >= 1),
                error_code TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (event_id) REFERENCES governance_outbox_events(event_id)
                    ON DELETE RESTRICT
            ) WITHOUT ROWID
            """,
            """
            CREATE TABLE governance_operator_holds (
                hold_id TEXT PRIMARY KEY NOT NULL,
                scope_kind TEXT NOT NULL CHECK (scope_kind = 'outbox_destination'),
                scope_ref TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                FOREIGN KEY (source_event_id) REFERENCES governance_outbox_events(event_id)
                    ON DELETE RESTRICT,
                CHECK (resolved_at IS NULL)
            ) WITHOUT ROWID
            """,
        ),
    ),
    Migration(
        version=7,
        name="decision-outbox-integrity-roots",
        statements=(
            "DROP TRIGGER governance_audit_events_no_update",
            "ALTER TABLE governance_audit_events ADD COLUMN destination_manifest_digest TEXT",
            "ALTER TABLE governance_audit_events ADD COLUMN destination_count INTEGER",
            """
            CREATE TRIGGER governance_action_tokens_no_delete
            BEFORE DELETE ON governance_action_tokens
            BEGIN
                SELECT RAISE(ABORT, 'governance action token is durable');
            END
            """,
            """
            CREATE TRIGGER governance_action_tokens_immutable_issuance
            BEFORE UPDATE ON governance_action_tokens
            WHEN OLD.token_id != NEW.token_id
              OR OLD.token_hash != NEW.token_hash
              OR OLD.project_namespace != NEW.project_namespace
              OR OLD.project_id != NEW.project_id
              OR OLD.proposal_id != NEW.proposal_id
              OR OLD.active_definition_digest != NEW.active_definition_digest
              OR OLD.content_revision != NEW.content_revision
              OR OLD.state_revision != NEW.state_revision
              OR OLD.decision_epoch != NEW.decision_epoch
              OR OLD.allowed_action != NEW.allowed_action
              OR OLD.allowed_actor_id != NEW.allowed_actor_id
              OR OLD.allowed_actor_type != NEW.allowed_actor_type
              OR OLD.bound_channel_json != NEW.bound_channel_json
              OR OLD.issued_at != NEW.issued_at
              OR OLD.expires_at != NEW.expires_at
            BEGIN
                SELECT RAISE(ABORT, 'governance action token issuance is immutable');
            END
            """,
            """
            CREATE TRIGGER governance_decision_results_no_update
            BEFORE UPDATE ON governance_decision_results
            BEGIN
                SELECT RAISE(ABORT, 'governance decision result is append-only');
            END
            """,
            """
            CREATE TRIGGER governance_decision_results_no_delete
            BEFORE DELETE ON governance_decision_results
            BEGIN
                SELECT RAISE(ABORT, 'governance decision result is append-only');
            END
            """,
            """
            CREATE TRIGGER governance_outbox_dead_letters_no_update
            BEFORE UPDATE ON governance_outbox_dead_letters
            BEGIN
                SELECT RAISE(ABORT, 'governance dead letter is append-only');
            END
            """,
            """
            CREATE TRIGGER governance_outbox_dead_letters_no_delete
            BEFORE DELETE ON governance_outbox_dead_letters
            BEGIN
                SELECT RAISE(ABORT, 'governance dead letter is append-only');
            END
            """,
            """
            CREATE TRIGGER governance_operator_holds_no_update
            BEFORE UPDATE ON governance_operator_holds
            BEGIN
                SELECT RAISE(ABORT, 'governance operator hold is append-only');
            END
            """,
            """
            CREATE TRIGGER governance_operator_holds_no_delete
            BEFORE DELETE ON governance_operator_holds
            BEGIN
                SELECT RAISE(ABORT, 'governance operator hold is append-only');
            END
            """,
        ),
    ),
    Migration(
        version=8,
        name="audit-manifest-integrity-finalize",
        statements=(
            """
            CREATE TRIGGER governance_audit_manifest_required
            BEFORE INSERT ON governance_audit_events
            WHEN NEW.destination_manifest_digest IS NULL
              OR length(NEW.destination_manifest_digest) != 71
              OR substr(NEW.destination_manifest_digest, 1, 7) != 'sha256:'
              OR substr(NEW.destination_manifest_digest, 8) GLOB '*[^0-9a-f]*'
              OR NEW.destination_count IS NULL
              OR NEW.destination_count < 1
            BEGIN
                SELECT RAISE(ABORT, 'governance audit manifest is required');
            END
            """,
            """
            CREATE TRIGGER governance_audit_events_no_update
            BEFORE UPDATE ON governance_audit_events
            BEGIN
                SELECT RAISE(ABORT, 'governance audit is append-only');
            END
            """,
        ),
    ),
    Migration(
        version=9,
        name="apply-grant-job-foundation",
        statements=(
            """
            CREATE TABLE governance_approved_snapshots (
                snapshot_id TEXT PRIMARY KEY NOT NULL,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                definition_digest TEXT NOT NULL,
                snapshot_digest TEXT NOT NULL UNIQUE,
                content_revision INTEGER NOT NULL CHECK (content_revision >= 1),
                state_revision INTEGER NOT NULL CHECK (state_revision >= 2),
                decision_epoch INTEGER NOT NULL CHECK (decision_epoch >= 1),
                expected_base_revision TEXT NOT NULL,
                approved_at TEXT NOT NULL,
                UNIQUE (
                    project_namespace, project_id, proposal_id,
                    definition_digest, content_revision, decision_epoch
                ),
                UNIQUE (
                    snapshot_id, project_namespace, project_id, proposal_id,
                    snapshot_digest, content_revision, state_revision,
                    decision_epoch
                ),
                UNIQUE (
                    snapshot_id, project_namespace, project_id, proposal_id,
                    snapshot_digest, expected_base_revision
                ),
                UNIQUE (snapshot_id, project_namespace, project_id, proposal_id),
                FOREIGN KEY (project_namespace, project_id, proposal_id)
                    REFERENCES governance_active_proposals(
                        project_namespace, project_id, proposal_id
                    ) ON DELETE RESTRICT,
                CHECK (
                    length(definition_digest) = 71
                    AND substr(definition_digest, 1, 7) = 'sha256:'
                    AND substr(definition_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(snapshot_digest) = 71
                    AND substr(snapshot_digest, 1, 7) = 'sha256:'
                    AND substr(snapshot_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(expected_base_revision) BETWEEN 7 AND 64
                    AND expected_base_revision NOT GLOB '*[^0-9a-f]*'
                )
            ) WITHOUT ROWID
            """,
            """
            CREATE TRIGGER governance_approved_snapshots_no_update
            BEFORE UPDATE ON governance_approved_snapshots
            BEGIN
                SELECT RAISE(ABORT, 'approved snapshot is immutable');
            END
            """,
            """
            CREATE TRIGGER governance_approved_snapshots_no_delete
            BEFORE DELETE ON governance_approved_snapshots
            BEGIN
                SELECT RAISE(ABORT, 'approved snapshot is durable');
            END
            """,
            """
            CREATE TABLE governance_apply_grants (
                grant_id TEXT PRIMARY KEY NOT NULL,
                grant_hash TEXT NOT NULL UNIQUE,
                snapshot_id TEXT NOT NULL,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                approved_snapshot_digest TEXT NOT NULL,
                content_revision INTEGER NOT NULL CHECK (content_revision >= 1),
                state_revision INTEGER NOT NULL CHECK (state_revision >= 2),
                decision_epoch INTEGER NOT NULL CHECK (decision_epoch >= 1),
                allowed_action TEXT NOT NULL CHECK (allowed_action = 'request_apply'),
                allowed_actor_id TEXT NOT NULL,
                allowed_actor_type TEXT NOT NULL CHECK (allowed_actor_type = 'human'),
                bound_channel_json TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('issued', 'consumed', 'expired', 'revoked')),
                resolved_at TEXT,
                FOREIGN KEY (
                    snapshot_id, project_namespace, project_id, proposal_id,
                    approved_snapshot_digest, content_revision, state_revision,
                    decision_epoch
                ) REFERENCES governance_approved_snapshots(
                    snapshot_id, project_namespace, project_id, proposal_id,
                    snapshot_digest, content_revision, state_revision, decision_epoch
                ) ON DELETE RESTRICT,
                CHECK (
                    length(grant_hash) = 71
                    AND substr(grant_hash, 1, 7) = 'sha256:'
                    AND substr(grant_hash, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(approved_snapshot_digest) = 71
                    AND substr(approved_snapshot_digest, 1, 7) = 'sha256:'
                    AND substr(approved_snapshot_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    (state = 'issued' AND resolved_at IS NULL)
                    OR (state != 'issued' AND resolved_at IS NOT NULL)
                ),
                CHECK (expires_at > issued_at)
            ) WITHOUT ROWID
            """,
            """
            CREATE TRIGGER governance_apply_grants_immutable_issuance
            BEFORE UPDATE ON governance_apply_grants
            WHEN OLD.grant_id != NEW.grant_id
              OR OLD.grant_hash != NEW.grant_hash
              OR OLD.snapshot_id != NEW.snapshot_id
              OR OLD.project_namespace != NEW.project_namespace
              OR OLD.project_id != NEW.project_id
              OR OLD.proposal_id != NEW.proposal_id
              OR OLD.approved_snapshot_digest != NEW.approved_snapshot_digest
              OR OLD.content_revision != NEW.content_revision
              OR OLD.state_revision != NEW.state_revision
              OR OLD.decision_epoch != NEW.decision_epoch
              OR OLD.allowed_action != NEW.allowed_action
              OR OLD.allowed_actor_id != NEW.allowed_actor_id
              OR OLD.allowed_actor_type != NEW.allowed_actor_type
              OR OLD.bound_channel_json != NEW.bound_channel_json
              OR OLD.issued_at != NEW.issued_at
              OR OLD.expires_at != NEW.expires_at
            BEGIN
                SELECT RAISE(ABORT, 'apply grant issuance is immutable');
            END
            """,
            """
            CREATE TRIGGER governance_apply_grants_no_delete
            BEFORE DELETE ON governance_apply_grants
            BEGIN
                SELECT RAISE(ABORT, 'apply grant is durable');
            END
            """,
            """
            CREATE UNIQUE INDEX governance_one_issued_apply_grant
            ON governance_apply_grants(
                snapshot_id, allowed_action, allowed_actor_id, bound_channel_json
            ) WHERE state = 'issued'
            """,
            """
            CREATE TABLE governance_apply_jobs (
                job_id TEXT PRIMARY KEY NOT NULL,
                snapshot_id TEXT NOT NULL,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                approved_snapshot_digest TEXT NOT NULL,
                expected_base_revision TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'queued', 'leased', 'running', 'retry_wait', 'staged',
                        'publish_pending', 'succeeded', 'dead_letter', 'recovery_hold'
                    )
                ),
                attempts INTEGER NOT NULL CHECK (attempts >= 0),
                fencing_token INTEGER NOT NULL CHECK (fencing_token >= 0),
                lease_owner TEXT,
                lease_expires_at TEXT,
                retry_at TEXT,
                staged_artifact_digest TEXT,
                publish_request_digest TEXT,
                last_error_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (
                    snapshot_id, project_namespace, project_id, proposal_id,
                    approved_snapshot_digest, expected_base_revision
                ) REFERENCES governance_approved_snapshots(
                    snapshot_id, project_namespace, project_id, proposal_id,
                    snapshot_digest, expected_base_revision
                ) ON DELETE RESTRICT,
                CHECK (
                    length(approved_snapshot_digest) = 71
                    AND substr(approved_snapshot_digest, 1, 7) = 'sha256:'
                    AND substr(approved_snapshot_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(expected_base_revision) BETWEEN 7 AND 64
                    AND expected_base_revision NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    staged_artifact_digest IS NULL OR (
                        length(staged_artifact_digest) = 71
                        AND substr(staged_artifact_digest, 1, 7) = 'sha256:'
                        AND substr(staged_artifact_digest, 8) NOT GLOB '*[^0-9a-f]*'
                    )
                ),
                CHECK (
                    publish_request_digest IS NULL OR (
                        length(publish_request_digest) = 71
                        AND substr(publish_request_digest, 1, 7) = 'sha256:'
                        AND substr(publish_request_digest, 8) NOT GLOB '*[^0-9a-f]*'
                    )
                ),
                CHECK (
                    (status IN ('leased', 'running')
                     AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
                    OR (status NOT IN ('leased', 'running')
                     AND lease_owner IS NULL AND lease_expires_at IS NULL)
                ),
                CHECK (
                    (status = 'retry_wait' AND retry_at IS NOT NULL)
                    OR (status != 'retry_wait' AND retry_at IS NULL)
                )
            ) WITHOUT ROWID
            """,
            """
            CREATE UNIQUE INDEX governance_one_active_apply_job
            ON governance_apply_jobs(project_namespace, project_id, proposal_id)
            WHERE status IN (
                'queued', 'leased', 'running', 'retry_wait', 'staged',
                'publish_pending', 'recovery_hold'
            )
            """,
            """
            CREATE UNIQUE INDEX governance_apply_job_result_identity
            ON governance_apply_jobs(
                job_id, snapshot_id, project_namespace, project_id, proposal_id
            )
            """,
            """
            CREATE TRIGGER governance_apply_job_identity_immutable
            BEFORE UPDATE ON governance_apply_jobs
            WHEN OLD.job_id != NEW.job_id
              OR OLD.snapshot_id != NEW.snapshot_id
              OR OLD.project_namespace != NEW.project_namespace
              OR OLD.project_id != NEW.project_id
              OR OLD.proposal_id != NEW.proposal_id
              OR OLD.approved_snapshot_digest != NEW.approved_snapshot_digest
              OR OLD.expected_base_revision != NEW.expected_base_revision
              OR OLD.created_at != NEW.created_at
            BEGIN
                SELECT RAISE(ABORT, 'apply job identity is immutable');
            END
            """,
            """
            CREATE TRIGGER governance_apply_jobs_no_delete
            BEFORE DELETE ON governance_apply_jobs
            BEGIN
                SELECT RAISE(ABORT, 'apply job is durable');
            END
            """,
            """
            CREATE TABLE governance_apply_request_results (
                idempotency_key TEXT PRIMARY KEY NOT NULL,
                request_fingerprint TEXT NOT NULL,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                job_id TEXT NOT NULL UNIQUE,
                actor_id TEXT NOT NULL,
                actor_type TEXT NOT NULL CHECK (actor_type = 'human'),
                channel_json TEXT NOT NULL,
                proposal_status TEXT NOT NULL CHECK (proposal_status = 'apply_requested'),
                processed_at TEXT NOT NULL,
                FOREIGN KEY (snapshot_id, project_namespace, project_id, proposal_id)
                    REFERENCES governance_approved_snapshots(
                        snapshot_id, project_namespace, project_id, proposal_id
                    ) ON DELETE RESTRICT,
                FOREIGN KEY (
                    job_id, snapshot_id, project_namespace, project_id, proposal_id
                ) REFERENCES governance_apply_jobs(
                    job_id, snapshot_id, project_namespace, project_id, proposal_id
                ) ON DELETE RESTRICT,
                CHECK (
                    length(request_fingerprint) = 64
                    AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
                )
            ) WITHOUT ROWID
            """,
            """
            CREATE TRIGGER governance_apply_request_results_no_update
            BEFORE UPDATE ON governance_apply_request_results
            BEGIN
                SELECT RAISE(ABORT, 'apply request result is append-only');
            END
            """,
            """
            CREATE TRIGGER governance_apply_request_results_no_delete
            BEFORE DELETE ON governance_apply_request_results
            BEGIN
                SELECT RAISE(ABORT, 'apply request result is durable');
            END
            """,
        ),
    ),
    Migration(
        version=10,
        name="apply-request-grant-root",
        statements=(
            """
            CREATE UNIQUE INDEX governance_apply_grant_result_identity
            ON governance_apply_grants(
                grant_id, snapshot_id, project_namespace, project_id, proposal_id,
                allowed_actor_id, allowed_actor_type, bound_channel_json
            )
            """,
            """
            ALTER TABLE governance_apply_request_results
            RENAME TO governance_apply_request_results_v9
            """,
            """
            CREATE TABLE governance_apply_request_results (
                idempotency_key TEXT PRIMARY KEY NOT NULL,
                request_fingerprint TEXT NOT NULL,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                grant_id TEXT NOT NULL UNIQUE,
                job_id TEXT NOT NULL UNIQUE,
                actor_id TEXT NOT NULL,
                actor_type TEXT NOT NULL CHECK (actor_type = 'human'),
                channel_json TEXT NOT NULL,
                proposal_status TEXT NOT NULL CHECK (proposal_status = 'apply_requested'),
                processed_at TEXT NOT NULL,
                FOREIGN KEY (snapshot_id, project_namespace, project_id, proposal_id)
                    REFERENCES governance_approved_snapshots(
                        snapshot_id, project_namespace, project_id, proposal_id
                    ) ON DELETE RESTRICT,
                FOREIGN KEY (
                    grant_id, snapshot_id, project_namespace, project_id, proposal_id,
                    actor_id, actor_type, channel_json
                ) REFERENCES governance_apply_grants(
                    grant_id, snapshot_id, project_namespace, project_id, proposal_id,
                    allowed_actor_id, allowed_actor_type, bound_channel_json
                ) ON DELETE RESTRICT,
                FOREIGN KEY (
                    job_id, snapshot_id, project_namespace, project_id, proposal_id
                ) REFERENCES governance_apply_jobs(
                    job_id, snapshot_id, project_namespace, project_id, proposal_id
                ) ON DELETE RESTRICT,
                CHECK (
                    length(request_fingerprint) = 64
                    AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
                )
            ) WITHOUT ROWID
            """,
            """
            INSERT INTO governance_apply_request_results(
                idempotency_key, request_fingerprint, project_namespace, project_id,
                proposal_id, snapshot_id, grant_id, job_id, actor_id, actor_type,
                channel_json, proposal_status, processed_at
            )
            SELECT r.idempotency_key, r.request_fingerprint, r.project_namespace,
                   r.project_id, r.proposal_id, r.snapshot_id,
                   (
                       SELECT g.grant_id FROM governance_apply_grants g
                       WHERE g.snapshot_id = r.snapshot_id
                         AND g.project_namespace = r.project_namespace
                         AND g.project_id = r.project_id
                         AND g.proposal_id = r.proposal_id
                         AND g.allowed_actor_id = r.actor_id
                         AND g.allowed_actor_type = r.actor_type
                         AND g.bound_channel_json = r.channel_json
                         AND g.state = 'consumed'
                       ORDER BY g.resolved_at DESC, g.grant_id
                       LIMIT 1
                   ),
                   r.job_id, r.actor_id, r.actor_type, r.channel_json,
                   r.proposal_status, r.processed_at
            FROM governance_apply_request_results_v9 r
            """,
            "DROP TABLE governance_apply_request_results_v9",
            """
            CREATE TRIGGER governance_apply_request_results_no_update
            BEFORE UPDATE ON governance_apply_request_results
            BEGIN
                SELECT RAISE(ABORT, 'apply request result is append-only');
            END
            """,
            """
            CREATE TRIGGER governance_apply_request_results_no_delete
            BEFORE DELETE ON governance_apply_request_results
            BEGIN
                SELECT RAISE(ABORT, 'apply request result is durable');
            END
            """,
        ),
    ),
    Migration(
        version=11,
        name="apply-job-lifecycle-roots",
        statements=(
            """
            CREATE TABLE governance_apply_job_events (
                job_event_id TEXT PRIMARY KEY NOT NULL,
                command_id TEXT NOT NULL UNIQUE,
                job_id TEXT NOT NULL,
                event_sequence INTEGER NOT NULL CHECK (event_sequence >= 1),
                snapshot_id TEXT NOT NULL,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK (event_type IN (
                    'claimed', 'started', 'heartbeat', 'retry_scheduled',
                    'publish_prepared', 'dead_lettered'
                )),
                worker_id TEXT NOT NULL,
                before_status TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL CHECK (attempts >= 1),
                fencing_token INTEGER NOT NULL CHECK (fencing_token >= 1),
                lease_owner TEXT,
                lease_expires_at TEXT,
                retry_at TEXT,
                staged_artifact_digest TEXT,
                publish_request_digest TEXT,
                last_error_code TEXT,
                payload_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (job_id, event_sequence),
                FOREIGN KEY (
                    job_id, snapshot_id, project_namespace, project_id, proposal_id
                ) REFERENCES governance_apply_jobs(
                    job_id, snapshot_id, project_namespace, project_id, proposal_id
                ) ON DELETE RESTRICT,
                CHECK (length(payload_digest) = 71 AND substr(payload_digest, 1, 7) = 'sha256:'),
                CHECK (staged_artifact_digest IS NULL OR length(staged_artifact_digest) = 71),
                CHECK (publish_request_digest IS NULL OR length(publish_request_digest) = 71)
            ) WITHOUT ROWID
            """,
            """
            CREATE TRIGGER governance_apply_job_events_no_update
            BEFORE UPDATE ON governance_apply_job_events
            BEGIN SELECT RAISE(ABORT, 'apply job event is append-only'); END
            """,
            """
            CREATE TRIGGER governance_apply_job_events_no_delete
            BEFORE DELETE ON governance_apply_job_events
            BEGIN SELECT RAISE(ABORT, 'apply job event is durable'); END
            """,
            """
            CREATE TABLE governance_staging_artifacts (
                job_id TEXT NOT NULL,
                fencing_token INTEGER NOT NULL CHECK (fencing_token >= 1),
                artifact_digest TEXT NOT NULL,
                artifact_bytes BLOB NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (job_id, fencing_token),
                FOREIGN KEY (job_id) REFERENCES governance_apply_jobs(job_id) ON DELETE RESTRICT,
                CHECK (length(artifact_digest) = 71 AND substr(artifact_digest, 1, 7) = 'sha256:')
            ) WITHOUT ROWID
            """,
            """
            CREATE TRIGGER governance_staging_artifacts_no_update
            BEFORE UPDATE ON governance_staging_artifacts
            BEGIN SELECT RAISE(ABORT, 'staging artifact is immutable'); END
            """,
            """
            CREATE TRIGGER governance_staging_artifacts_no_delete
            BEFORE DELETE ON governance_staging_artifacts
            BEGIN SELECT RAISE(ABORT, 'staging artifact is durable'); END
            """,
            """
            CREATE TABLE governance_publish_inputs (
                job_id TEXT NOT NULL,
                fencing_token INTEGER NOT NULL CHECK (fencing_token >= 1),
                publish_request_digest TEXT NOT NULL,
                publish_request_bytes BLOB NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (job_id, fencing_token),
                FOREIGN KEY (job_id) REFERENCES governance_apply_jobs(job_id) ON DELETE RESTRICT,
                CHECK (
                    length(publish_request_digest) = 71
                    AND substr(publish_request_digest, 1, 7) = 'sha256:'
                )
            ) WITHOUT ROWID
            """,
            """
            CREATE TRIGGER governance_publish_inputs_no_update
            BEFORE UPDATE ON governance_publish_inputs
            BEGIN SELECT RAISE(ABORT, 'publish input is immutable'); END
            """,
            """
            CREATE TRIGGER governance_publish_inputs_no_delete
            BEFORE DELETE ON governance_publish_inputs
            BEGIN SELECT RAISE(ABORT, 'publish input is durable'); END
            """,
        ),
    ),
    Migration(
        version=12,
        name="fenced-publish-foundation",
        statements=(
            """
            CREATE UNIQUE INDEX governance_apply_job_publish_identity
            ON governance_apply_jobs(
                job_id, snapshot_id, project_namespace, project_id, proposal_id,
                fencing_token, approved_snapshot_digest, expected_base_revision,
                staged_artifact_digest, publish_request_digest
            )
            """,
            """
            CREATE UNIQUE INDEX governance_staging_artifact_publish_identity
            ON governance_staging_artifacts(job_id, fencing_token, artifact_digest)
            """,
            """
            CREATE UNIQUE INDEX governance_publish_input_identity
            ON governance_publish_inputs(job_id, fencing_token, publish_request_digest)
            """,
            """
            CREATE TABLE governance_publish_intents (
                intent_id TEXT PRIMARY KEY NOT NULL,
                job_id TEXT NOT NULL UNIQUE,
                snapshot_id TEXT NOT NULL,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                fencing_token INTEGER NOT NULL CHECK (fencing_token >= 1),
                approved_snapshot_digest TEXT NOT NULL,
                expected_base_revision TEXT NOT NULL,
                staged_artifact_digest TEXT NOT NULL,
                publish_request_digest TEXT NOT NULL,
                canonical_ref TEXT NOT NULL,
                expected_old_ref TEXT NOT NULL,
                candidate_commit TEXT NOT NULL,
                candidate_tree_digest TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'prepared', 'published', 'publish_conflict',
                        'cancelled', 'recovery_hold', 'failed'
                    )
                ),
                prepared_at TEXT NOT NULL,
                resolved_at TEXT,
                last_error_code TEXT,
                UNIQUE (
                    intent_id, job_id, snapshot_id, project_namespace, project_id,
                    proposal_id, fencing_token, expected_old_ref, candidate_commit
                ),
                FOREIGN KEY (
                    job_id, snapshot_id, project_namespace, project_id, proposal_id,
                    fencing_token, approved_snapshot_digest, expected_base_revision,
                    staged_artifact_digest, publish_request_digest
                ) REFERENCES governance_apply_jobs(
                    job_id, snapshot_id, project_namespace, project_id, proposal_id,
                    fencing_token, approved_snapshot_digest, expected_base_revision,
                    staged_artifact_digest, publish_request_digest
                ) ON DELETE RESTRICT,
                FOREIGN KEY (job_id, fencing_token, staged_artifact_digest)
                    REFERENCES governance_staging_artifacts(
                        job_id, fencing_token, artifact_digest
                    ) ON DELETE RESTRICT,
                FOREIGN KEY (job_id, fencing_token, publish_request_digest)
                    REFERENCES governance_publish_inputs(
                        job_id, fencing_token, publish_request_digest
                    ) ON DELETE RESTRICT,
                CHECK (
                    length(intent_id) = 20
                    AND substr(intent_id, 1, 4) = 'PBI-'
                    AND substr(intent_id, 5) NOT GLOB '*[^A-F0-9]*'
                ),
                CHECK (
                    length(approved_snapshot_digest) = 71
                    AND substr(approved_snapshot_digest, 1, 7) = 'sha256:'
                    AND substr(approved_snapshot_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(staged_artifact_digest) = 71
                    AND substr(staged_artifact_digest, 1, 7) = 'sha256:'
                    AND substr(staged_artifact_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(publish_request_digest) = 71
                    AND substr(publish_request_digest, 1, 7) = 'sha256:'
                    AND substr(publish_request_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(candidate_tree_digest) = 71
                    AND substr(candidate_tree_digest, 1, 7) = 'sha256:'
                    AND substr(candidate_tree_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(expected_base_revision) BETWEEN 7 AND 64
                    AND expected_base_revision NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(expected_old_ref) IN (40, 64)
                    AND expected_old_ref NOT GLOB '*[^0-9a-f]*'
                    AND substr(expected_old_ref, 1, length(expected_base_revision))
                        = expected_base_revision
                ),
                CHECK (
                    length(candidate_commit) IN (40, 64)
                    AND candidate_commit NOT GLOB '*[^0-9a-f]*'
                    AND length(candidate_commit) = length(expected_old_ref)
                    AND candidate_commit != expected_old_ref
                ),
                CHECK (
                    length(canonical_ref) BETWEEN 12 AND 255
                    AND substr(canonical_ref, 1, 11) = 'refs/heads/'
                    AND canonical_ref NOT GLOB '*[^ -~]*'
                    AND instr(canonical_ref, ' ') = 0
                    AND instr(canonical_ref, '~') = 0
                    AND instr(canonical_ref, '^') = 0
                    AND instr(canonical_ref, ':') = 0
                    AND instr(canonical_ref, '?') = 0
                    AND instr(canonical_ref, '*') = 0
                    AND instr(canonical_ref, '[') = 0
                    AND instr(canonical_ref, '\\') = 0
                    AND substr(canonical_ref, -1) != '/'
                    AND substr(canonical_ref, -1) != '.'
                    AND instr(canonical_ref, '/.') = 0
                    AND instr(canonical_ref, '.lock/') = 0
                    AND substr(canonical_ref, -5) != '.lock'
                    AND instr(canonical_ref, '..') = 0
                    AND instr(canonical_ref, '//') = 0
                    AND instr(canonical_ref, '@{') = 0
                ),
                CHECK (
                    (status IN ('prepared', 'recovery_hold') AND resolved_at IS NULL)
                    OR (
                        status IN ('published', 'publish_conflict', 'cancelled', 'failed')
                        AND resolved_at IS NOT NULL
                    )
                ),
                CHECK (
                    (status IN ('prepared', 'published', 'cancelled')
                     AND last_error_code IS NULL)
                    OR (status IN ('publish_conflict', 'recovery_hold', 'failed')
                     AND last_error_code IS NOT NULL)
                )
            ) WITHOUT ROWID
            """,
            """
            CREATE UNIQUE INDEX governance_one_active_publish_intent
            ON governance_publish_intents(project_namespace, project_id)
            WHERE status IN ('prepared', 'recovery_hold')
            """,
            """
            CREATE TRIGGER governance_publish_intent_identity_immutable
            BEFORE UPDATE ON governance_publish_intents
            WHEN OLD.intent_id != NEW.intent_id
              OR OLD.job_id != NEW.job_id
              OR OLD.snapshot_id != NEW.snapshot_id
              OR OLD.project_namespace != NEW.project_namespace
              OR OLD.project_id != NEW.project_id
              OR OLD.proposal_id != NEW.proposal_id
              OR OLD.fencing_token != NEW.fencing_token
              OR OLD.approved_snapshot_digest != NEW.approved_snapshot_digest
              OR OLD.expected_base_revision != NEW.expected_base_revision
              OR OLD.staged_artifact_digest != NEW.staged_artifact_digest
              OR OLD.publish_request_digest != NEW.publish_request_digest
              OR OLD.canonical_ref != NEW.canonical_ref
              OR OLD.expected_old_ref != NEW.expected_old_ref
              OR OLD.candidate_commit != NEW.candidate_commit
              OR OLD.candidate_tree_digest != NEW.candidate_tree_digest
              OR OLD.prepared_at != NEW.prepared_at
            BEGIN SELECT RAISE(ABORT, 'publish intent identity is immutable'); END
            """,
            """
            CREATE TRIGGER governance_publish_intent_transition_guard
            BEFORE UPDATE ON governance_publish_intents
            WHEN OLD.status != NEW.status AND NOT (
                (OLD.status = 'prepared' AND NEW.status IN (
                    'published', 'publish_conflict', 'cancelled', 'recovery_hold', 'failed'
                ))
                OR (OLD.status = 'recovery_hold' AND NEW.status IN (
                    'prepared', 'published', 'publish_conflict', 'cancelled', 'failed'
                ))
            )
            BEGIN SELECT RAISE(ABORT, 'invalid publish intent transition'); END
            """,
            """
            CREATE TRIGGER governance_publish_intent_state_requires_transition
            BEFORE UPDATE ON governance_publish_intents
            WHEN OLD.status = NEW.status AND (
                OLD.resolved_at IS NOT NEW.resolved_at
                OR OLD.last_error_code IS NOT NEW.last_error_code
            )
            BEGIN SELECT RAISE(ABORT, 'publish intent state requires transition'); END
            """,
            """
            CREATE TRIGGER governance_publish_intents_no_delete
            BEFORE DELETE ON governance_publish_intents
            BEGIN SELECT RAISE(ABORT, 'publish intent is durable'); END
            """,
            """
            CREATE TABLE governance_project_publish_gates (
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                canonical_ref TEXT NOT NULL,
                active_intent_id TEXT UNIQUE,
                gate_revision INTEGER NOT NULL CHECK (gate_revision >= 1),
                state TEXT NOT NULL CHECK (state IN ('unlocked', 'locked', 'recovery_hold')),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (project_namespace, project_id),
                FOREIGN KEY (active_intent_id)
                    REFERENCES governance_publish_intents(intent_id) ON DELETE RESTRICT,
                CHECK (
                    (state = 'unlocked' AND active_intent_id IS NULL)
                    OR (state IN ('locked', 'recovery_hold') AND active_intent_id IS NOT NULL)
                ),
                CHECK (
                    length(canonical_ref) BETWEEN 12 AND 255
                    AND substr(canonical_ref, 1, 11) = 'refs/heads/'
                    AND canonical_ref NOT GLOB '*[^ -~]*'
                    AND instr(canonical_ref, ' ') = 0
                    AND instr(canonical_ref, '~') = 0
                    AND instr(canonical_ref, '^') = 0
                    AND instr(canonical_ref, ':') = 0
                    AND instr(canonical_ref, '?') = 0
                    AND instr(canonical_ref, '*') = 0
                    AND instr(canonical_ref, '[') = 0
                    AND instr(canonical_ref, '\\') = 0
                    AND substr(canonical_ref, -1) != '/'
                    AND substr(canonical_ref, -1) != '.'
                    AND instr(canonical_ref, '/.') = 0
                    AND instr(canonical_ref, '.lock/') = 0
                    AND substr(canonical_ref, -5) != '.lock'
                    AND instr(canonical_ref, '..') = 0
                    AND instr(canonical_ref, '//') = 0
                    AND instr(canonical_ref, '@{') = 0
                )
            ) WITHOUT ROWID
            """,
            """
            CREATE TRIGGER governance_project_publish_gate_identity_immutable
            BEFORE UPDATE ON governance_project_publish_gates
            WHEN OLD.project_namespace != NEW.project_namespace
              OR OLD.project_id != NEW.project_id
              OR OLD.canonical_ref != NEW.canonical_ref
              OR NEW.gate_revision != OLD.gate_revision + 1
            BEGIN SELECT RAISE(ABORT, 'publish gate identity/revision is immutable'); END
            """,
            """
            CREATE TRIGGER governance_project_publish_gate_insert_consistency
            BEFORE INSERT ON governance_project_publish_gates
            WHEN NEW.state != 'unlocked' AND NOT EXISTS (
                SELECT 1 FROM governance_publish_intents i
                WHERE i.intent_id = NEW.active_intent_id
                  AND i.project_namespace = NEW.project_namespace
                  AND i.project_id = NEW.project_id
                  AND i.canonical_ref = NEW.canonical_ref
                  AND (
                      (NEW.state = 'locked' AND i.status = 'prepared')
                      OR (NEW.state = 'recovery_hold' AND i.status = 'recovery_hold')
                  )
            )
            BEGIN SELECT RAISE(ABORT, 'publish gate does not match active intent'); END
            """,
            """
            CREATE TRIGGER governance_project_publish_gate_update_consistency
            BEFORE UPDATE ON governance_project_publish_gates
            WHEN (
                NEW.state = 'unlocked' AND EXISTS (
                    SELECT 1 FROM governance_publish_intents i
                    WHERE i.intent_id = OLD.active_intent_id
                      AND i.status IN ('prepared', 'recovery_hold')
                )
            ) OR (
                NEW.state != 'unlocked' AND NOT EXISTS (
                    SELECT 1 FROM governance_publish_intents i
                    WHERE i.intent_id = NEW.active_intent_id
                      AND i.project_namespace = NEW.project_namespace
                      AND i.project_id = NEW.project_id
                      AND i.canonical_ref = NEW.canonical_ref
                      AND (
                          (NEW.state = 'locked' AND i.status = 'prepared')
                          OR (NEW.state = 'recovery_hold' AND i.status = 'recovery_hold')
                      )
                )
            )
            BEGIN SELECT RAISE(ABORT, 'publish gate transition is inconsistent'); END
            """,
            """
            CREATE TRIGGER governance_project_publish_gates_no_delete
            BEFORE DELETE ON governance_project_publish_gates
            BEGIN SELECT RAISE(ABORT, 'publish gate is durable'); END
            """,
            """
            CREATE TABLE governance_publish_results (
                intent_id TEXT PRIMARY KEY NOT NULL,
                job_id TEXT NOT NULL UNIQUE,
                snapshot_id TEXT NOT NULL,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                fencing_token INTEGER NOT NULL,
                expected_old_ref TEXT NOT NULL,
                candidate_commit TEXT NOT NULL,
                actual_ref TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK (
                    outcome IN ('published', 'publish_conflict', 'cancelled', 'failed')
                ),
                error_code TEXT,
                resolved_at TEXT NOT NULL,
                FOREIGN KEY (
                    intent_id, job_id, snapshot_id, project_namespace, project_id,
                    proposal_id, fencing_token, expected_old_ref, candidate_commit
                ) REFERENCES governance_publish_intents(
                    intent_id, job_id, snapshot_id, project_namespace, project_id,
                    proposal_id, fencing_token, expected_old_ref, candidate_commit
                ) ON DELETE RESTRICT,
                CHECK (
                    length(actual_ref) IN (40, 64)
                    AND actual_ref NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    (outcome IN ('published', 'cancelled') AND error_code IS NULL)
                    OR (outcome IN ('publish_conflict', 'failed') AND error_code IS NOT NULL)
                ),
                CHECK (outcome != 'published' OR actual_ref = candidate_commit),
                CHECK (outcome != 'cancelled' OR actual_ref = expected_old_ref)
            ) WITHOUT ROWID
            """,
            """
            CREATE TRIGGER governance_publish_result_intent_consistency
            BEFORE INSERT ON governance_publish_results
            WHEN NOT EXISTS (
                SELECT 1 FROM governance_publish_intents i
                WHERE i.intent_id = NEW.intent_id
                  AND i.job_id = NEW.job_id
                  AND i.snapshot_id = NEW.snapshot_id
                  AND i.project_namespace = NEW.project_namespace
                  AND i.project_id = NEW.project_id
                  AND i.proposal_id = NEW.proposal_id
                  AND i.fencing_token = NEW.fencing_token
                  AND i.expected_old_ref = NEW.expected_old_ref
                  AND i.candidate_commit = NEW.candidate_commit
                  AND i.status = NEW.outcome
                  AND i.resolved_at = NEW.resolved_at
                  AND i.last_error_code IS NEW.error_code
            )
            BEGIN SELECT RAISE(ABORT, 'publish result does not match terminal intent'); END
            """,
            """
            CREATE TRIGGER governance_publish_results_no_update
            BEFORE UPDATE ON governance_publish_results
            BEGIN SELECT RAISE(ABORT, 'publish result is append-only'); END
            """,
            """
            CREATE TRIGGER governance_publish_results_no_delete
            BEFORE DELETE ON governance_publish_results
            BEGIN SELECT RAISE(ABORT, 'publish result is durable'); END
            """,
        ),
    ),
    Migration(
        version=13,
        name="durable-publish-coordinator-claim",
        statements=(
            """
            CREATE UNIQUE INDEX governance_publish_intent_claim_identity
            ON governance_publish_intents(intent_id, project_namespace, project_id)
            """,
            """
            CREATE TABLE governance_publish_claims (
                claim_id TEXT PRIMARY KEY NOT NULL,
                intent_id TEXT NOT NULL,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                coordinator_id TEXT NOT NULL,
                claim_fencing_token INTEGER NOT NULL CHECK (claim_fencing_token >= 1),
                state TEXT NOT NULL CHECK (state IN ('active', 'released')),
                claimed_at TEXT NOT NULL,
                resolved_at TEXT,
                UNIQUE (intent_id, claim_fencing_token),
                FOREIGN KEY (intent_id, project_namespace, project_id)
                    REFERENCES governance_publish_intents(
                        intent_id, project_namespace, project_id
                    ) ON DELETE RESTRICT,
                CHECK (
                    length(claim_id) = 20
                    AND substr(claim_id, 1, 4) = 'PCL-'
                    AND substr(claim_id, 5) NOT GLOB '*[^A-F0-9]*'
                ),
                CHECK (length(coordinator_id) BETWEEN 1 AND 128),
                CHECK (
                    (state = 'active' AND resolved_at IS NULL)
                    OR (state = 'released' AND resolved_at IS NOT NULL)
                )
            ) WITHOUT ROWID
            """,
            """
            CREATE UNIQUE INDEX governance_one_active_publish_claim
            ON governance_publish_claims(project_namespace, project_id)
            WHERE state = 'active'
            """,
            """
            CREATE UNIQUE INDEX governance_one_active_publish_intent_claim
            ON governance_publish_claims(intent_id)
            WHERE state = 'active'
            """,
            """
            CREATE TRIGGER governance_publish_claim_identity_immutable
            BEFORE UPDATE ON governance_publish_claims
            WHEN OLD.claim_id != NEW.claim_id
              OR OLD.intent_id != NEW.intent_id
              OR OLD.project_namespace != NEW.project_namespace
              OR OLD.project_id != NEW.project_id
              OR OLD.coordinator_id != NEW.coordinator_id
              OR OLD.claim_fencing_token != NEW.claim_fencing_token
              OR OLD.claimed_at != NEW.claimed_at
            BEGIN SELECT RAISE(ABORT, 'publish claim identity is immutable'); END
            """,
            """
            CREATE TRIGGER governance_publish_claim_transition_guard
            BEFORE UPDATE ON governance_publish_claims
            WHEN NOT (OLD.state = 'active' AND NEW.state = 'released')
            BEGIN SELECT RAISE(ABORT, 'invalid publish claim transition'); END
            """,
            """
            CREATE TRIGGER governance_publish_claims_no_delete
            BEFORE DELETE ON governance_publish_claims
            BEGIN SELECT RAISE(ABORT, 'publish claim is durable'); END
            """,
            """
            CREATE TRIGGER governance_publish_claim_blocks_intent_transition
            BEFORE UPDATE ON governance_publish_intents
            WHEN OLD.status = 'prepared' AND NEW.status != 'prepared'
             AND EXISTS (
                SELECT 1 FROM governance_publish_claims c
                WHERE c.intent_id = OLD.intent_id AND c.state = 'active'
             )
            BEGIN SELECT RAISE(ABORT, 'active publish claim blocks intent transition'); END
            """,
            """
            CREATE TRIGGER governance_publish_claim_blocks_gate_transition
            BEFORE UPDATE ON governance_project_publish_gates
            WHEN EXISTS (
                SELECT 1 FROM governance_publish_claims c
                WHERE c.intent_id = OLD.active_intent_id AND c.state = 'active'
            )
            BEGIN SELECT RAISE(ABORT, 'active publish claim blocks gate transition'); END
            """,
        ),
    ),
    Migration(
        version=14,
        name="authoritative-publish-resolution",
        statements=(
            """
            ALTER TABLE governance_active_proposals
            ADD COLUMN applied_revision TEXT CHECK (
                applied_revision IS NULL OR (
                    length(applied_revision) IN (40, 64)
                    AND applied_revision NOT GLOB '*[^0-9a-f]*'
                )
            )
            """,
            """
            DROP TRIGGER governance_publish_claim_blocks_intent_transition
            """,
            """
            CREATE TRIGGER governance_publish_claim_blocks_intent_transition
            BEFORE UPDATE ON governance_publish_intents
            WHEN OLD.status IN ('prepared', 'recovery_hold')
             AND NEW.status != OLD.status
             AND EXISTS (
                SELECT 1 FROM governance_publish_claims c
                WHERE c.intent_id = OLD.intent_id AND c.state = 'active'
             )
            BEGIN SELECT RAISE(ABORT, 'active publish claim blocks intent transition'); END
            """,
            """
            CREATE TABLE governance_publish_resolution_events (
                resolution_event_id TEXT PRIMARY KEY NOT NULL,
                command_id TEXT NOT NULL UNIQUE,
                intent_id TEXT NOT NULL,
                resolution_sequence INTEGER NOT NULL CHECK (resolution_sequence >= 1),
                claim_id TEXT NOT NULL,
                resolver_id TEXT NOT NULL CHECK (length(resolver_id) BETWEEN 1 AND 128),
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                resolution_type TEXT NOT NULL CHECK (
                    resolution_type IN (
                        'published', 'publish_conflict', 'recovery_hold',
                        'failed', 'cancelled', 'retry_released'
                    )
                ),
                actual_ref TEXT,
                intent_status TEXT NOT NULL CHECK (
                    intent_status IN (
                        'prepared', 'published', 'publish_conflict',
                        'cancelled', 'recovery_hold', 'failed'
                    )
                ),
                job_status TEXT NOT NULL CHECK (
                    job_status IN ('publish_pending', 'succeeded', 'dead_letter', 'recovery_hold')
                ),
                proposal_status TEXT NOT NULL CHECK (
                    proposal_status IN ('apply_requested', 'apply_failed', 'applied')
                ),
                proposal_state_revision INTEGER NOT NULL CHECK (proposal_state_revision >= 3),
                applied_revision TEXT,
                error_code TEXT,
                payload_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (intent_id, resolution_sequence),
                FOREIGN KEY (intent_id) REFERENCES governance_publish_intents(intent_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (claim_id) REFERENCES governance_publish_claims(claim_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (job_id) REFERENCES governance_apply_jobs(job_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (project_namespace, project_id, proposal_id)
                    REFERENCES governance_active_proposals(
                        project_namespace, project_id, proposal_id
                    ) ON DELETE RESTRICT,
                CHECK (
                    actual_ref IS NULL OR (
                        length(actual_ref) IN (40, 64)
                        AND actual_ref NOT GLOB '*[^0-9a-f]*'
                    )
                ),
                CHECK (
                    applied_revision IS NULL OR (
                        length(applied_revision) IN (40, 64)
                        AND applied_revision NOT GLOB '*[^0-9a-f]*'
                    )
                ),
                CHECK (length(payload_digest) = 71 AND substr(payload_digest, 1, 7) = 'sha256:'),
                CHECK (
                    (resolution_type = 'published'
                     AND intent_status = 'published' AND job_status = 'succeeded'
                     AND proposal_status = 'applied' AND actual_ref = applied_revision
                     AND error_code IS NULL)
                    OR (resolution_type = 'publish_conflict'
                     AND intent_status = 'publish_conflict' AND job_status = 'recovery_hold'
                     AND proposal_status = 'apply_requested' AND actual_ref IS NOT NULL
                     AND applied_revision IS NULL AND error_code IS NOT NULL)
                    OR (resolution_type = 'recovery_hold'
                     AND intent_status = 'recovery_hold' AND job_status = 'recovery_hold'
                     AND proposal_status = 'apply_requested' AND applied_revision IS NULL
                     AND error_code IS NOT NULL)
                    OR (resolution_type = 'failed'
                     AND intent_status = 'failed' AND job_status = 'dead_letter'
                     AND proposal_status = 'apply_failed' AND actual_ref IS NOT NULL
                     AND applied_revision IS NULL AND error_code IS NOT NULL)
                    OR (resolution_type = 'cancelled'
                     AND intent_status = 'cancelled' AND job_status = 'dead_letter'
                     AND proposal_status = 'apply_failed' AND actual_ref IS NOT NULL
                     AND applied_revision IS NULL AND error_code IS NOT NULL)
                    OR (resolution_type = 'retry_released'
                     AND intent_status = 'prepared' AND job_status = 'publish_pending'
                     AND proposal_status = 'apply_requested' AND actual_ref IS NOT NULL
                     AND applied_revision IS NULL AND error_code IS NULL)
                )
            ) WITHOUT ROWID
            """,
            """
            CREATE TRIGGER governance_publish_resolution_events_no_update
            BEFORE UPDATE ON governance_publish_resolution_events
            BEGIN SELECT RAISE(ABORT, 'publish resolution event is append-only'); END
            """,
            """
            CREATE TRIGGER governance_publish_resolution_events_no_delete
            BEFORE DELETE ON governance_publish_resolution_events
            BEGIN SELECT RAISE(ABORT, 'publish resolution event is durable'); END
            """,
        ),
    ),
    Migration(
        version=15,
        name="publish-resolution-composite-roots",
        statements=(
            """
            CREATE UNIQUE INDEX governance_publish_claim_composite_identity
            ON governance_publish_claims(claim_id, intent_id)
            """,
            """
            CREATE UNIQUE INDEX governance_publish_intent_composite_identity
            ON governance_publish_intents(
                intent_id, job_id, project_namespace, project_id, proposal_id
            )
            """,
            """
            CREATE UNIQUE INDEX governance_apply_job_composite_identity
            ON governance_apply_jobs(job_id, project_namespace, project_id, proposal_id)
            """,
            """
            CREATE UNIQUE INDEX governance_publish_resolution_composite_identity
            ON governance_publish_resolution_events(
                resolution_event_id, intent_id, claim_id, job_id,
                project_namespace, project_id, proposal_id
            )
            """,
            """
            CREATE UNIQUE INDEX governance_one_terminal_publish_resolution
            ON governance_publish_resolution_events(intent_id)
            WHERE resolution_type IN ('published', 'publish_conflict', 'failed', 'cancelled')
            """,
            """
            CREATE TABLE governance_publish_resolution_roots (
                resolution_event_id TEXT PRIMARY KEY NOT NULL,
                intent_id TEXT NOT NULL,
                claim_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                before_job_status TEXT NOT NULL CHECK (
                    before_job_status IN ('publish_pending', 'recovery_hold')
                ),
                stream_revision INTEGER NOT NULL CHECK (stream_revision >= 1),
                FOREIGN KEY (
                    resolution_event_id, intent_id, claim_id, job_id,
                    project_namespace, project_id, proposal_id
                ) REFERENCES governance_publish_resolution_events(
                    resolution_event_id, intent_id, claim_id, job_id,
                    project_namespace, project_id, proposal_id
                ) ON DELETE RESTRICT,
                FOREIGN KEY (claim_id, intent_id)
                    REFERENCES governance_publish_claims(claim_id, intent_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (intent_id, job_id, project_namespace, project_id, proposal_id)
                    REFERENCES governance_publish_intents(
                        intent_id, job_id, project_namespace, project_id, proposal_id
                    ) ON DELETE RESTRICT,
                FOREIGN KEY (job_id, project_namespace, project_id, proposal_id)
                    REFERENCES governance_apply_jobs(
                        job_id, project_namespace, project_id, proposal_id
                    ) ON DELETE RESTRICT,
                FOREIGN KEY (project_namespace, project_id, proposal_id)
                    REFERENCES governance_active_proposals(
                        project_namespace, project_id, proposal_id
                    ) ON DELETE RESTRICT,
                UNIQUE (job_id, stream_revision)
            ) WITHOUT ROWID
            """,
            """
            INSERT INTO governance_publish_resolution_roots(
                resolution_event_id, intent_id, claim_id, job_id,
                project_namespace, project_id, proposal_id,
                before_job_status, stream_revision
            )
            SELECT e.resolution_event_id, e.intent_id, e.claim_id, e.job_id,
                   e.project_namespace, e.project_id, e.proposal_id,
                   COALESCE(
                       (SELECT prior.job_status
                        FROM governance_publish_resolution_events prior
                        WHERE prior.intent_id = e.intent_id
                          AND prior.resolution_sequence = e.resolution_sequence - 1),
                       'publish_pending'
                   ),
                   (SELECT COALESCE(MAX(j.event_sequence), 0)
                    FROM governance_apply_job_events j WHERE j.job_id = e.job_id)
                   + e.resolution_sequence
            FROM governance_publish_resolution_events e
            """,
            """
            CREATE TRIGGER governance_publish_resolution_roots_no_update
            BEFORE UPDATE ON governance_publish_resolution_roots
            BEGIN SELECT RAISE(ABORT, 'publish resolution root is append-only'); END
            """,
            """
            CREATE TRIGGER governance_publish_resolution_roots_no_delete
            BEFORE DELETE ON governance_publish_resolution_roots
            BEGIN SELECT RAISE(ABORT, 'publish resolution root is durable'); END
            """,
            """
            CREATE TRIGGER governance_active_proposal_applied_revision_insert_guard
            BEFORE INSERT ON governance_active_proposals
            WHEN (NEW.status = 'applied') != (NEW.applied_revision IS NOT NULL)
            BEGIN SELECT RAISE(ABORT, 'applied proposal revision invariant'); END
            """,
            """
            CREATE TRIGGER governance_active_proposal_applied_revision_update_guard
            BEFORE UPDATE ON governance_active_proposals
            WHEN (NEW.status = 'applied') != (NEW.applied_revision IS NOT NULL)
            BEGIN SELECT RAISE(ABORT, 'applied proposal revision invariant'); END
            """,
            """
            DROP TRIGGER governance_publish_intent_state_requires_transition
            """,
            """
            CREATE TRIGGER governance_publish_intent_state_requires_transition
            BEFORE UPDATE ON governance_publish_intents
            WHEN OLD.status = NEW.status
             AND NOT (
                 OLD.status = 'recovery_hold'
                 AND OLD.resolved_at IS NEW.resolved_at
                 AND NEW.resolved_at IS NULL
             )
             AND (
                 OLD.resolved_at IS NOT NEW.resolved_at
                 OR OLD.last_error_code IS NOT NEW.last_error_code
             )
            BEGIN SELECT RAISE(ABORT, 'publish intent state requires transition'); END
            """,
        ),
    ),
    Migration(
        version=16,
        name="legacy-publish-resolution-compatibility",
        statements=(
            """
            DROP TRIGGER governance_publish_resolution_roots_no_update
            """,
            """
            DROP TRIGGER governance_publish_resolution_roots_no_delete
            """,
            """
            UPDATE governance_publish_resolution_roots
            SET before_job_status = 'publish_pending',
                stream_revision = (
                    SELECT e.resolution_sequence
                    FROM governance_publish_resolution_events e
                    WHERE e.resolution_event_id =
                          governance_publish_resolution_roots.resolution_event_id
                )
            WHERE EXISTS (
                SELECT 1 FROM governance_publish_resolution_events e
                WHERE e.resolution_event_id =
                      governance_publish_resolution_roots.resolution_event_id
                  AND instr(e.payload_json, '\"stream_revision\"') = 0
            )
            """,
            """
            CREATE TRIGGER governance_publish_resolution_roots_no_update
            BEFORE UPDATE ON governance_publish_resolution_roots
            BEGIN SELECT RAISE(ABORT, 'publish resolution root is append-only'); END
            """,
            """
            CREATE TRIGGER governance_publish_resolution_roots_no_delete
            BEFORE DELETE ON governance_publish_resolution_roots
            BEGIN SELECT RAISE(ABORT, 'publish resolution root is durable'); END
            """,
        ),
    ),
    Migration(
        version=17,
        name="legacy-publish-outbox-revision-repair",
        statements=(
            """
            DROP TRIGGER governance_publish_resolution_roots_no_update
            """,
            """
            DROP TRIGGER governance_publish_resolution_roots_no_delete
            """,
            """
            DROP TRIGGER governance_outbox_payload_immutable
            """,
            """
            UPDATE governance_publish_resolution_roots
            SET stream_revision = (
                SELECT COALESCE(MAX(j.event_sequence), 0) + e.resolution_sequence
                FROM governance_publish_resolution_events e
                LEFT JOIN governance_apply_job_events j ON j.job_id = e.job_id
                WHERE e.resolution_event_id =
                      governance_publish_resolution_roots.resolution_event_id
            )
            WHERE EXISTS (
                SELECT 1 FROM governance_publish_resolution_events e
                WHERE e.resolution_event_id =
                      governance_publish_resolution_roots.resolution_event_id
                  AND instr(e.payload_json, '\"stream_revision\"') = 0
            )
            """,
            """
            UPDATE governance_outbox_events
            SET source_state_revision = (
                SELECT COALESCE(MAX(j.event_sequence), 0) + e.resolution_sequence
                FROM governance_audit_events a
                JOIN governance_publish_resolution_events e
                  ON e.command_id = a.command_id
                LEFT JOIN governance_apply_job_events j ON j.job_id = e.job_id
                WHERE a.project_namespace = governance_outbox_events.project_namespace
                  AND a.project_id = governance_outbox_events.project_id
                  AND a.proposal_id = governance_outbox_events.proposal_id
                  AND a.aggregate_sequence = governance_outbox_events.aggregate_sequence
            )
            WHERE EXISTS (
                SELECT 1
                FROM governance_audit_events a
                JOIN governance_publish_resolution_events e
                  ON e.command_id = a.command_id
                WHERE a.project_namespace = governance_outbox_events.project_namespace
                  AND a.project_id = governance_outbox_events.project_id
                  AND a.proposal_id = governance_outbox_events.proposal_id
                  AND a.aggregate_sequence = governance_outbox_events.aggregate_sequence
                  AND instr(e.payload_json, '\"stream_revision\"') = 0
            )
            """,
            """
            CREATE TRIGGER governance_outbox_payload_immutable
            BEFORE UPDATE ON governance_outbox_events
            WHEN OLD.project_namespace != NEW.project_namespace
              OR OLD.project_id != NEW.project_id
              OR OLD.proposal_id != NEW.proposal_id
              OR OLD.aggregate_sequence != NEW.aggregate_sequence
              OR OLD.destination_ref != NEW.destination_ref
              OR OLD.destination_sequence != NEW.destination_sequence
              OR OLD.source_state_revision != NEW.source_state_revision
              OR OLD.supersession_key IS NOT NEW.supersession_key
              OR OLD.payload_digest != NEW.payload_digest
              OR OLD.payload_json != NEW.payload_json
              OR OLD.created_at != NEW.created_at
            BEGIN
                SELECT RAISE(ABORT, 'outbox payload is immutable');
            END
            """,
            """
            CREATE TRIGGER governance_publish_resolution_roots_no_update
            BEFORE UPDATE ON governance_publish_resolution_roots
            BEGIN SELECT RAISE(ABORT, 'publish resolution root is append-only'); END
            """,
            """
            CREATE TRIGGER governance_publish_resolution_roots_no_delete
            BEFORE DELETE ON governance_publish_resolution_roots
            BEGIN SELECT RAISE(ABORT, 'publish resolution root is durable'); END
            """,
        ),
    ),
    Migration(
        version=18,
        name="legacy-proposal-migration-state",
        statements=(
            """
            CREATE TABLE governance_legacy_migrations (
                migration_id TEXT PRIMARY KEY NOT NULL,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                freeze_id TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                snapshot_digest TEXT NOT NULL,
                plan_digest TEXT NOT NULL UNIQUE,
                mapping_policy_version INTEGER NOT NULL CHECK (mapping_policy_version >= 1),
                base_revision TEXT NOT NULL,
                validation_policy_ref TEXT NOT NULL,
                project_pack_backup_path TEXT NOT NULL,
                project_pack_backup_digest TEXT NOT NULL,
                governance_backup_path TEXT NOT NULL,
                governance_backup_digest TEXT NOT NULL,
                proposal_count INTEGER NOT NULL CHECK (proposal_count >= 1),
                status TEXT NOT NULL CHECK (status IN ('prepared', 'state_imported')),
                prepared_at TEXT NOT NULL,
                state_imported_at TEXT,
                CHECK (
                    length(migration_id) = 20
                    AND substr(migration_id, 1, 4) = 'MPL-'
                    AND substr(migration_id, 5) NOT GLOB '*[^A-F0-9]*'
                ),
                CHECK (
                    length(snapshot_id) = 20
                    AND substr(snapshot_id, 1, 4) = 'MPS-'
                    AND substr(snapshot_id, 5) NOT GLOB '*[^A-F0-9]*'
                ),
                CHECK (
                    length(snapshot_digest) = 71
                    AND substr(snapshot_digest, 1, 7) = 'sha256:'
                    AND substr(snapshot_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(plan_digest) = 71
                    AND substr(plan_digest, 1, 7) = 'sha256:'
                    AND substr(plan_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(project_pack_backup_digest) = 71
                    AND substr(project_pack_backup_digest, 1, 7) = 'sha256:'
                    AND substr(project_pack_backup_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(governance_backup_digest) = 71
                    AND substr(governance_backup_digest, 1, 7) = 'sha256:'
                    AND substr(governance_backup_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    (status = 'prepared' AND state_imported_at IS NULL)
                    OR (status = 'state_imported' AND state_imported_at IS NOT NULL)
                )
            ) WITHOUT ROWID
            """,
            """
            CREATE TABLE governance_legacy_migration_items (
                migration_id TEXT NOT NULL,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                source_status TEXT NOT NULL,
                source_revision INTEGER NOT NULL CHECK (source_revision >= 1),
                target_status TEXT NOT NULL,
                definition_digest TEXT NOT NULL,
                proposal_artifact_digest TEXT NOT NULL,
                content_revision INTEGER NOT NULL CHECK (content_revision >= 1),
                state_revision INTEGER NOT NULL CHECK (state_revision >= 1),
                decision_epoch INTEGER NOT NULL CHECK (decision_epoch >= 1),
                approval_disposition TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                PRIMARY KEY (migration_id, project_namespace, project_id, proposal_id),
                UNIQUE (project_namespace, project_id, proposal_id),
                FOREIGN KEY (migration_id)
                    REFERENCES governance_legacy_migrations(migration_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (project_namespace, project_id, proposal_id)
                    REFERENCES governance_active_proposals(
                        project_namespace, project_id, proposal_id
                    )
                    ON DELETE RESTRICT,
                CHECK (
                    source_status IN (
                        'draft', 'reviewed', 'changes_requested', 'approved',
                        'applied', 'rejected', 'superseded'
                    )
                ),
                CHECK (
                    target_status IN (
                        'draft', 'reviewed', 'changes_requested', 'approved',
                        'applied', 'rejected', 'superseded',
                        'legacy_approval_review_required'
                    )
                ),
                CHECK (
                    approval_disposition IN (
                        'not_required', 'legacy_audit_present', 'synthetic_required'
                    )
                ),
                CHECK (
                    length(definition_digest) = 71
                    AND substr(definition_digest, 1, 7) = 'sha256:'
                    AND substr(definition_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(proposal_artifact_digest) = 71
                    AND substr(proposal_artifact_digest, 1, 7) = 'sha256:'
                    AND substr(proposal_artifact_digest, 8) NOT GLOB '*[^0-9a-f]*'
                )
            ) WITHOUT ROWID
            """,
        ),
    ),
    Migration(
        version=19,
        name="legacy-migration-durability",
        statements=(
            """
            CREATE UNIQUE INDEX governance_legacy_migrations_identity
            ON governance_legacy_migrations(migration_id, project_namespace, project_id)
            """,
            """
            ALTER TABLE governance_legacy_migration_items
            RENAME TO governance_legacy_migration_items_v18
            """,
            """
            CREATE TABLE governance_legacy_migration_items (
                migration_id TEXT NOT NULL,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                source_status TEXT NOT NULL,
                source_revision INTEGER NOT NULL CHECK (source_revision >= 1),
                target_status TEXT NOT NULL,
                definition_digest TEXT NOT NULL,
                proposal_artifact_digest TEXT NOT NULL,
                content_revision INTEGER NOT NULL CHECK (content_revision >= 1),
                state_revision INTEGER NOT NULL CHECK (state_revision >= 1),
                decision_epoch INTEGER NOT NULL CHECK (decision_epoch >= 1),
                approval_disposition TEXT NOT NULL,
                legacy_git_revision TEXT,
                imported_at TEXT NOT NULL,
                PRIMARY KEY (migration_id, project_namespace, project_id, proposal_id),
                UNIQUE (project_namespace, project_id, proposal_id),
                FOREIGN KEY (migration_id, project_namespace, project_id)
                    REFERENCES governance_legacy_migrations(
                        migration_id, project_namespace, project_id
                    )
                    ON DELETE RESTRICT,
                CHECK (
                    source_status IN (
                        'draft', 'reviewed', 'changes_requested', 'approved',
                        'applied', 'rejected', 'superseded'
                    )
                ),
                CHECK (
                    target_status IN (
                        'draft', 'reviewed', 'changes_requested', 'approved',
                        'applied', 'rejected', 'superseded',
                        'legacy_approval_review_required'
                    )
                ),
                CHECK (
                    approval_disposition IN (
                        'not_required', 'legacy_audit_present', 'synthetic_required'
                    )
                ),
                CHECK (
                    length(definition_digest) = 71
                    AND substr(definition_digest, 1, 7) = 'sha256:'
                    AND substr(definition_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(proposal_artifact_digest) = 71
                    AND substr(proposal_artifact_digest, 1, 7) = 'sha256:'
                    AND substr(proposal_artifact_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    legacy_git_revision IS NULL
                    OR (
                        length(legacy_git_revision) BETWEEN 7 AND 40
                        AND legacy_git_revision NOT GLOB '*[^0-9a-f]*'
                    )
                )
            ) WITHOUT ROWID
            """,
            """
            INSERT INTO governance_legacy_migration_items(
                migration_id, project_namespace, project_id, proposal_id,
                source_status, source_revision, target_status, definition_digest,
                proposal_artifact_digest, content_revision, state_revision,
                decision_epoch, approval_disposition, legacy_git_revision, imported_at
            )
            SELECT migration_id, project_namespace, project_id, proposal_id,
                   source_status, source_revision, target_status, definition_digest,
                   proposal_artifact_digest, content_revision, state_revision,
                   decision_epoch, approval_disposition,
                   CASE
                       WHEN source_status = 'applied' THEN (
                           SELECT p.applied_revision
                           FROM governance_active_proposals p
                           WHERE p.project_namespace = i.project_namespace
                             AND p.project_id = i.project_id
                             AND p.proposal_id = i.proposal_id
                             AND p.active_definition_digest = i.definition_digest
                             AND p.content_revision = i.content_revision
                             AND p.state_revision = i.state_revision
                             AND p.decision_epoch = i.decision_epoch
                             AND p.status = CASE i.target_status
                                 WHEN 'legacy_approval_review_required'
                                     THEN 'changes_requested'
                                 ELSE i.target_status
                             END
                       )
                       ELSE NULL
                   END,
                   imported_at
            FROM governance_legacy_migration_items_v18 i
            """,
            """
            UPDATE governance_active_proposals
            SET status = 'draft', applied_revision = NULL
            WHERE EXISTS (
                SELECT 1
                FROM governance_legacy_migration_items i
                WHERE i.project_namespace = governance_active_proposals.project_namespace
                  AND i.project_id = governance_active_proposals.project_id
                  AND i.proposal_id = governance_active_proposals.proposal_id
                  AND i.target_status NOT IN ('draft', 'reviewed')
                  AND i.definition_digest =
                      governance_active_proposals.active_definition_digest
                  AND i.content_revision = governance_active_proposals.content_revision
                  AND i.state_revision = governance_active_proposals.state_revision
                  AND i.decision_epoch = governance_active_proposals.decision_epoch
                  AND governance_active_proposals.status = CASE i.target_status
                      WHEN 'legacy_approval_review_required' THEN 'changes_requested'
                      ELSE i.target_status
                  END
            )
            """,
            """
            DROP TABLE governance_legacy_migration_items_v18
            """,
            """
            CREATE TRIGGER governance_legacy_migration_items_insert_guard
            BEFORE INSERT ON governance_legacy_migration_items
            WHEN NOT EXISTS (
                    SELECT 1 FROM governance_active_proposals p
                    WHERE p.project_namespace = NEW.project_namespace
                      AND p.project_id = NEW.project_id
                      AND p.proposal_id = NEW.proposal_id
                 )
            BEGIN SELECT RAISE(ABORT, 'legacy migration item requires active proposal'); END
            """,
            """
            CREATE TRIGGER governance_legacy_migrations_insert_guard
            BEFORE INSERT ON governance_legacy_migrations
            WHEN NEW.status != 'prepared' OR NEW.state_imported_at IS NOT NULL
            BEGIN SELECT RAISE(ABORT, 'legacy migration root must start prepared'); END
            """,
            """
            CREATE TRIGGER governance_legacy_migrations_transition_guard
            BEFORE UPDATE ON governance_legacy_migrations
            WHEN OLD.migration_id != NEW.migration_id
              OR OLD.project_namespace != NEW.project_namespace
              OR OLD.project_id != NEW.project_id
              OR OLD.freeze_id != NEW.freeze_id
              OR OLD.snapshot_id != NEW.snapshot_id
              OR OLD.snapshot_digest != NEW.snapshot_digest
              OR OLD.plan_digest != NEW.plan_digest
              OR OLD.mapping_policy_version != NEW.mapping_policy_version
              OR OLD.base_revision != NEW.base_revision
              OR OLD.validation_policy_ref != NEW.validation_policy_ref
              OR OLD.project_pack_backup_path != NEW.project_pack_backup_path
              OR OLD.project_pack_backup_digest != NEW.project_pack_backup_digest
              OR OLD.governance_backup_path != NEW.governance_backup_path
              OR OLD.governance_backup_digest != NEW.governance_backup_digest
              OR OLD.proposal_count != NEW.proposal_count
              OR OLD.prepared_at != NEW.prepared_at
              OR OLD.status != 'prepared'
              OR NEW.status != 'state_imported'
              OR OLD.state_imported_at IS NOT NULL
              OR NEW.state_imported_at IS NULL
              OR (
                    SELECT COUNT(*)
                    FROM governance_legacy_migration_items i
                    WHERE i.migration_id = OLD.migration_id
                      AND i.project_namespace = OLD.project_namespace
                      AND i.project_id = OLD.project_id
                 ) != OLD.proposal_count
            BEGIN SELECT RAISE(ABORT, 'legacy migration root transition is invalid'); END
            """,
            """
            CREATE TRIGGER governance_legacy_migrations_no_delete
            BEFORE DELETE ON governance_legacy_migrations
            BEGIN SELECT RAISE(ABORT, 'legacy migration root is durable'); END
            """,
            """
            CREATE TRIGGER governance_legacy_migration_items_no_update
            BEFORE UPDATE ON governance_legacy_migration_items
            BEGIN SELECT RAISE(ABORT, 'legacy migration item is immutable'); END
            """,
            """
            CREATE TRIGGER governance_legacy_migration_items_no_delete
            BEFORE DELETE ON governance_legacy_migration_items
            BEGIN SELECT RAISE(ABORT, 'legacy migration item is durable'); END
            """,
        ),
    ),
    Migration(
        version=20,
        name="legacy-migration-audit-projection",
        statements=(
            """
            CREATE TABLE governance_legacy_import_commands (
                command_id TEXT PRIMARY KEY NOT NULL,
                migration_id TEXT NOT NULL,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                request_fingerprint TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK (
                    event_type IN (
                        'migration.legacy_approval',
                        'migration.synthetic_approval',
                        'migration.state_imported'
                    )
                ),
                actor_id TEXT NOT NULL,
                actor_type TEXT NOT NULL CHECK (actor_type IN ('human', 'service')),
                occurred_at TEXT NOT NULL,
                source_artifact_digest TEXT NOT NULL,
                reason TEXT,
                definition_digest TEXT NOT NULL,
                source_state_revision INTEGER NOT NULL CHECK (source_state_revision >= 1),
                payload_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE (migration_id, project_namespace, project_id, proposal_id),
                FOREIGN KEY (migration_id, project_namespace, project_id, proposal_id)
                    REFERENCES governance_legacy_migration_items(
                        migration_id, project_namespace, project_id, proposal_id
                    ) ON DELETE RESTRICT,
                CHECK (
                    length(command_id) = 20
                    AND substr(command_id, 1, 4) = 'MCM-'
                    AND substr(command_id, 5) NOT GLOB '*[^A-F0-9]*'
                ),
                CHECK (
                    length(request_fingerprint) = 64
                    AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(source_artifact_digest) = 71
                    AND substr(source_artifact_digest, 1, 7) = 'sha256:'
                    AND substr(source_artifact_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(definition_digest) = 71
                    AND substr(definition_digest, 1, 7) = 'sha256:'
                    AND substr(definition_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(payload_digest) = 71
                    AND substr(payload_digest, 1, 7) = 'sha256:'
                    AND substr(payload_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    (event_type = 'migration.synthetic_approval'
                     AND actor_type = 'service'
                     AND reason = 'legacy_approval_without_audit')
                    OR (event_type != 'migration.synthetic_approval' AND reason IS NULL)
                )
            ) WITHOUT ROWID
            """,
            """
            CREATE TABLE governance_legacy_approval_holds (
                migration_id TEXT NOT NULL,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                reason_code TEXT NOT NULL CHECK (
                    reason_code = 'legacy_approval_without_audit'
                ),
                source_artifact_digest TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (project_namespace, project_id, proposal_id),
                UNIQUE (migration_id, project_namespace, project_id, proposal_id),
                FOREIGN KEY (migration_id, project_namespace, project_id, proposal_id)
                    REFERENCES governance_legacy_migration_items(
                        migration_id, project_namespace, project_id, proposal_id
                    ) ON DELETE RESTRICT,
                CHECK (
                    length(source_artifact_digest) = 71
                    AND substr(source_artifact_digest, 1, 7) = 'sha256:'
                    AND substr(source_artifact_digest, 8) NOT GLOB '*[^0-9a-f]*'
                )
            ) WITHOUT ROWID
            """,
            """
            CREATE TABLE governance_legacy_event_backfill_pending (
                migration_id TEXT NOT NULL,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (migration_id, project_namespace, project_id, proposal_id),
                FOREIGN KEY (migration_id, project_namespace, project_id, proposal_id)
                    REFERENCES governance_legacy_migration_items(
                        migration_id, project_namespace, project_id, proposal_id
                    ) ON DELETE RESTRICT
            ) WITHOUT ROWID
            """,
            """
            CREATE TABLE governance_legacy_approval_reviews (
                review_id TEXT PRIMARY KEY NOT NULL,
                migration_id TEXT NOT NULL,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                request_fingerprint TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                actor_type TEXT NOT NULL CHECK (actor_type = 'human'),
                occurred_at TEXT NOT NULL,
                reason TEXT NOT NULL,
                request_id TEXT NOT NULL,
                channel_json TEXT NOT NULL,
                definition_digest TEXT NOT NULL,
                source_state_revision INTEGER NOT NULL CHECK (source_state_revision >= 2),
                payload_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE (migration_id, project_namespace, project_id, proposal_id),
                FOREIGN KEY (migration_id, project_namespace, project_id, proposal_id)
                    REFERENCES governance_legacy_approval_holds(
                        migration_id, project_namespace, project_id, proposal_id
                    ) ON DELETE RESTRICT,
                CHECK (
                    length(review_id) = 20
                    AND substr(review_id, 1, 4) = 'LAR-'
                    AND substr(review_id, 5) NOT GLOB '*[^A-F0-9]*'
                ),
                CHECK (
                    length(request_fingerprint) = 64
                    AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(definition_digest) = 71
                    AND substr(definition_digest, 1, 7) = 'sha256:'
                    AND substr(definition_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(payload_digest) = 71
                    AND substr(payload_digest, 1, 7) = 'sha256:'
                    AND substr(payload_digest, 8) NOT GLOB '*[^0-9a-f]*'
                )
            ) WITHOUT ROWID
            """,
            """
            INSERT INTO governance_legacy_event_backfill_pending(
                migration_id, project_namespace, project_id, proposal_id, created_at
            )
            SELECT migration_id, project_namespace, project_id, proposal_id,
                   strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            FROM governance_legacy_migration_items
            """,
            """
            CREATE TRIGGER governance_legacy_import_commands_no_update
            BEFORE UPDATE ON governance_legacy_import_commands
            BEGIN SELECT RAISE(ABORT, 'legacy import command is immutable'); END
            """,
            """
            CREATE TRIGGER governance_legacy_import_commands_no_delete
            BEFORE DELETE ON governance_legacy_import_commands
            BEGIN SELECT RAISE(ABORT, 'legacy import command is durable'); END
            """,
            """
            CREATE TRIGGER governance_legacy_approval_holds_no_update
            BEFORE UPDATE ON governance_legacy_approval_holds
            BEGIN SELECT RAISE(ABORT, 'legacy approval hold is immutable'); END
            """,
            """
            CREATE TRIGGER governance_legacy_approval_holds_no_delete
            BEFORE DELETE ON governance_legacy_approval_holds
            BEGIN SELECT RAISE(ABORT, 'legacy approval hold is durable'); END
            """,
            """
            CREATE TRIGGER governance_legacy_event_backfill_pending_no_update
            BEFORE UPDATE ON governance_legacy_event_backfill_pending
            BEGIN SELECT RAISE(ABORT, 'legacy event backfill marker is immutable'); END
            """,
            """
            CREATE TRIGGER governance_legacy_approval_reviews_no_update
            BEFORE UPDATE ON governance_legacy_approval_reviews
            BEGIN SELECT RAISE(ABORT, 'legacy approval review is immutable'); END
            """,
            """
            CREATE TRIGGER governance_legacy_approval_reviews_no_delete
            BEFORE DELETE ON governance_legacy_approval_reviews
            BEGIN SELECT RAISE(ABORT, 'legacy approval review is durable'); END
            """,
        ),
    ),
    Migration(
        version=21,
        name="legacy-migration-verification",
        statements=(
            """
            CREATE TABLE governance_legacy_migration_verifications (
                verification_id TEXT PRIMARY KEY NOT NULL,
                migration_id TEXT NOT NULL UNIQUE,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                snapshot_digest TEXT NOT NULL,
                plan_digest TEXT NOT NULL,
                proposal_count INTEGER NOT NULL CHECK (proposal_count >= 1),
                source_file_count INTEGER NOT NULL CHECK (source_file_count >= 1),
                report_digest TEXT NOT NULL UNIQUE,
                report_json TEXT NOT NULL,
                verified_by TEXT NOT NULL,
                verified_at TEXT NOT NULL,
                FOREIGN KEY (migration_id, project_namespace, project_id)
                    REFERENCES governance_legacy_migrations(
                        migration_id, project_namespace, project_id
                    ) ON DELETE RESTRICT,
                CHECK (
                    length(verification_id) = 20
                    AND substr(verification_id, 1, 4) = 'MVF-'
                    AND substr(verification_id, 5) NOT GLOB '*[^A-F0-9]*'
                ),
                CHECK (
                    length(snapshot_digest) = 71
                    AND substr(snapshot_digest, 1, 7) = 'sha256:'
                    AND substr(snapshot_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(plan_digest) = 71
                    AND substr(plan_digest, 1, 7) = 'sha256:'
                    AND substr(plan_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(report_digest) = 71
                    AND substr(report_digest, 1, 7) = 'sha256:'
                    AND substr(report_digest, 8) NOT GLOB '*[^0-9a-f]*'
                )
            ) WITHOUT ROWID
            """,
            """
            CREATE TRIGGER governance_legacy_migration_verifications_no_update
            BEFORE UPDATE ON governance_legacy_migration_verifications
            BEGIN SELECT RAISE(ABORT, 'legacy verification is immutable'); END
            """,
            """
            CREATE TRIGGER governance_legacy_migration_verifications_no_delete
            BEFORE DELETE ON governance_legacy_migration_verifications
            BEGIN SELECT RAISE(ABORT, 'legacy verification is durable'); END
            """,
        ),
    ),
    Migration(
        version=22,
        name="legacy-verification-root-binding",
        statements=(
            """
            CREATE TRIGGER governance_legacy_migration_verifications_insert_guard
            BEFORE INSERT ON governance_legacy_migration_verifications
            WHEN json_valid(NEW.report_json) != 1
              OR NOT EXISTS (
                    SELECT 1 FROM governance_legacy_migrations m
                    WHERE m.migration_id = NEW.migration_id
                      AND m.project_namespace = NEW.project_namespace
                      AND m.project_id = NEW.project_id
                      AND m.snapshot_id = NEW.snapshot_id
                      AND m.snapshot_digest = NEW.snapshot_digest
                      AND m.plan_digest = NEW.plan_digest
                      AND m.proposal_count = NEW.proposal_count
                      AND m.status = 'state_imported'
                )
              OR json_extract(NEW.report_json, '$.verification_id') IS NOT NEW.verification_id
              OR json_extract(NEW.report_json, '$.migration_id') IS NOT NEW.migration_id
              OR json_extract(NEW.report_json, '$.project_ref.namespace')
                    IS NOT NEW.project_namespace
              OR json_extract(NEW.report_json, '$.project_ref.project_id')
                    IS NOT NEW.project_id
              OR json_extract(NEW.report_json, '$.snapshot_id') IS NOT NEW.snapshot_id
              OR json_extract(NEW.report_json, '$.snapshot_digest') IS NOT NEW.snapshot_digest
              OR json_extract(NEW.report_json, '$.plan_digest') IS NOT NEW.plan_digest
              OR json_array_length(json_extract(NEW.report_json, '$.proposals'))
                    IS NOT NEW.proposal_count
              OR json_array_length(json_extract(NEW.report_json, '$.source_files'))
                    IS NOT NEW.source_file_count
              OR json_extract(NEW.report_json, '$.report_digest') IS NOT NEW.report_digest
              OR json_extract(NEW.report_json, '$.verified_by') IS NOT NEW.verified_by
              OR json_extract(NEW.report_json, '$.verified_at') IS NOT NEW.verified_at
              OR json_extract(NEW.report_json, '$.replayed') != 0
            BEGIN SELECT RAISE(ABORT, 'legacy verification root mismatch'); END
            """,
        ),
    ),
    Migration(
        version=23,
        name="legacy-verification-idempotency-binding",
        statements=(
            """
            CREATE TRIGGER governance_legacy_migration_verifications_idempotency_guard
            BEFORE INSERT ON governance_legacy_migration_verifications
            WHEN json_valid(NEW.report_json) = 1
              AND json_type(NEW.report_json, '$.proposals') IS 'array'
              AND EXISTS (
                    SELECT 1
                    FROM json_each(NEW.report_json, '$.proposals') AS proposal
                    WHERE json_type(proposal.value, '$.idempotency_key') IS NOT 'text'
                       OR length(json_extract(proposal.value, '$.idempotency_key')) = 0
                )
            BEGIN SELECT RAISE(
                ABORT,
                'legacy verification idempotency evidence is required'
            ); END
            """,
        ),
    ),
    Migration(
        version=24,
        name="legacy-migration-lifecycle-gate",
        statements=(
            """
            CREATE TABLE governance_legacy_migration_lifecycle_heads (
                migration_id TEXT PRIMARY KEY NOT NULL,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                verification_id TEXT,
                report_digest TEXT,
                state TEXT NOT NULL CHECK (
                    state IN (
                        'imported', 'staged_verified', 'activated',
                        'rolled_back', 'recovery_hold'
                    )
                ),
                lifecycle_revision INTEGER NOT NULL CHECK (lifecycle_revision >= 1),
                last_event_digest TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (migration_id, project_namespace, project_id)
                    REFERENCES governance_legacy_migrations(
                        migration_id, project_namespace, project_id
                    ) ON DELETE RESTRICT,
                FOREIGN KEY (verification_id)
                    REFERENCES governance_legacy_migration_verifications(verification_id)
                    ON DELETE RESTRICT,
                CHECK (
                    (state = 'imported' AND verification_id IS NULL AND report_digest IS NULL)
                    OR (state = 'activated' AND verification_id IS NULL AND report_digest IS NULL)
                    OR (
                        state NOT IN ('imported', 'activated')
                        AND verification_id IS NOT NULL
                        AND report_digest IS NOT NULL
                    )
                    OR (
                        state = 'activated'
                        AND verification_id IS NOT NULL
                        AND report_digest IS NOT NULL
                    )
                )
            ) WITHOUT ROWID
            """,
            """
            INSERT INTO governance_legacy_migration_lifecycle_heads(
                migration_id, project_namespace, project_id, verification_id,
                report_digest, state, lifecycle_revision, last_event_digest,
                created_at, updated_at
            )
            SELECT m.migration_id, m.project_namespace, m.project_id,
                   v.verification_id, v.report_digest,
                   CASE
                       WHEN v.verification_id IS NULL AND EXISTS (
                           SELECT 1
                           FROM governance_legacy_migration_items i
                           JOIN governance_active_proposals p
                             ON p.project_namespace = i.project_namespace
                            AND p.project_id = i.project_id
                            AND p.proposal_id = i.proposal_id
                           WHERE i.migration_id = m.migration_id
                             AND (
                                 p.active_definition_digest != i.definition_digest
                                 OR p.content_revision != i.content_revision
                                 OR p.state_revision != i.state_revision
                                 OR p.decision_epoch != i.decision_epoch
                                 OR p.status != CASE i.target_status
                                     WHEN 'reviewed' THEN 'reviewed'
                                     ELSE 'draft'
                                 END
                             )
                       ) THEN 'activated'
                       WHEN v.verification_id IS NULL AND EXISTS (
                           SELECT 1
                           FROM governance_legacy_migration_items i
                           JOIN governance_audit_events a
                             ON a.project_namespace = i.project_namespace
                            AND a.project_id = i.project_id
                            AND a.proposal_id = i.proposal_id
                           JOIN governance_outbox_events o
                             ON o.project_namespace = a.project_namespace
                            AND o.project_id = a.project_id
                            AND o.proposal_id = a.proposal_id
                            AND o.aggregate_sequence = a.aggregate_sequence
                           WHERE i.migration_id = m.migration_id
                             AND o.state != 'pending'
                       ) THEN 'activated'
                       WHEN v.verification_id IS NULL THEN 'imported'
                       WHEN EXISTS (
                           SELECT 1
                           FROM governance_legacy_migration_items i
                           JOIN governance_audit_events a
                             ON a.project_namespace = i.project_namespace
                            AND a.project_id = i.project_id
                            AND a.proposal_id = i.proposal_id
                           JOIN governance_outbox_events o
                             ON o.project_namespace = a.project_namespace
                            AND o.project_id = a.project_id
                            AND o.proposal_id = a.proposal_id
                            AND o.aggregate_sequence = a.aggregate_sequence
                           WHERE i.migration_id = m.migration_id
                             AND o.state != 'pending'
                       ) THEN 'recovery_hold'
                       ELSE 'staged_verified'
                   END,
                   1, NULL, m.prepared_at,
                   COALESCE(v.verified_at, m.state_imported_at, m.prepared_at)
            FROM governance_legacy_migrations m
            LEFT JOIN governance_legacy_migration_verifications v
              ON v.migration_id = m.migration_id
            WHERE m.status = 'state_imported'
            """,
            """
            CREATE TRIGGER governance_legacy_migration_lifecycle_heads_insert_guard
            BEFORE INSERT ON governance_legacy_migration_lifecycle_heads
            WHEN NEW.state != 'imported'
              OR NEW.verification_id IS NOT NULL
              OR NEW.report_digest IS NOT NULL
              OR NEW.lifecycle_revision != 1
              OR NEW.last_event_digest IS NOT NULL
              OR NOT EXISTS (
                    SELECT 1 FROM governance_legacy_migrations m
                    WHERE m.migration_id = NEW.migration_id
                      AND m.project_namespace = NEW.project_namespace
                      AND m.project_id = NEW.project_id
                      AND m.status = 'state_imported'
                )
            BEGIN SELECT RAISE(ABORT, 'legacy lifecycle head must start imported'); END
            """,
            """
            CREATE TRIGGER governance_legacy_migration_lifecycle_heads_update_guard
            BEFORE UPDATE ON governance_legacy_migration_lifecycle_heads
            WHEN OLD.migration_id != NEW.migration_id
              OR OLD.project_namespace != NEW.project_namespace
              OR OLD.project_id != NEW.project_id
              OR NEW.lifecycle_revision != OLD.lifecycle_revision + 1
              OR OLD.state IN ('activated', 'rolled_back', 'recovery_hold')
              OR NOT (
                    (OLD.state = 'imported' AND NEW.state = 'staged_verified'
                     AND OLD.verification_id IS NULL AND NEW.verification_id IS NOT NULL
                     AND OLD.report_digest IS NULL AND NEW.report_digest IS NOT NULL
                     AND NEW.last_event_digest IS NULL)
                    OR
                    (OLD.state = 'staged_verified'
                     AND NEW.state IN ('activated', 'rolled_back')
                     AND OLD.verification_id IS NEW.verification_id
                     AND OLD.report_digest IS NEW.report_digest
                     AND NEW.last_event_digest IS NOT NULL)
                )
            BEGIN SELECT RAISE(ABORT, 'legacy lifecycle transition is invalid'); END
            """,
            """
            CREATE TRIGGER governance_legacy_migration_lifecycle_heads_no_delete
            BEFORE DELETE ON governance_legacy_migration_lifecycle_heads
            BEGIN SELECT RAISE(ABORT, 'legacy lifecycle head is durable'); END
            """,
            """
            CREATE TRIGGER governance_legacy_import_creates_lifecycle_head
            AFTER UPDATE OF status ON governance_legacy_migrations
            WHEN OLD.status = 'prepared' AND NEW.status = 'state_imported'
            BEGIN
                INSERT INTO governance_legacy_migration_lifecycle_heads(
                    migration_id, project_namespace, project_id, verification_id,
                    report_digest, state, lifecycle_revision, last_event_digest,
                    created_at, updated_at
                ) VALUES (
                    NEW.migration_id, NEW.project_namespace, NEW.project_id,
                    NULL, NULL, 'imported', 1, NULL, NEW.prepared_at, NEW.state_imported_at
                );
            END
            """,
            """
            CREATE TRIGGER governance_legacy_verification_advances_lifecycle
            AFTER INSERT ON governance_legacy_migration_verifications
            BEGIN
                UPDATE governance_legacy_migration_lifecycle_heads
                SET verification_id = NEW.verification_id,
                    report_digest = NEW.report_digest,
                    state = 'staged_verified',
                    lifecycle_revision = lifecycle_revision + 1,
                    updated_at = NEW.verified_at
                WHERE migration_id = NEW.migration_id AND state = 'imported';
            END
            """,
            """
            CREATE TABLE governance_legacy_migration_lifecycle_commands (
                command_id TEXT PRIMARY KEY NOT NULL,
                migration_id TEXT NOT NULL,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                action TEXT NOT NULL CHECK (action IN ('activate', 'rollback')),
                expected_lifecycle_revision INTEGER NOT NULL CHECK (
                    expected_lifecycle_revision >= 1
                ),
                idempotency_key TEXT NOT NULL UNIQUE,
                request_fingerprint TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                actor_type TEXT NOT NULL CHECK (actor_type = 'human'),
                request_id TEXT NOT NULL,
                channel_json TEXT NOT NULL,
                reason TEXT NOT NULL CHECK (length(trim(reason)) >= 3),
                occurred_at TEXT NOT NULL,
                FOREIGN KEY (migration_id)
                    REFERENCES governance_legacy_migration_lifecycle_heads(migration_id)
                    ON DELETE RESTRICT
            ) WITHOUT ROWID
            """,
            """
            CREATE TABLE governance_legacy_migration_lifecycle_events (
                event_id TEXT PRIMARY KEY NOT NULL,
                command_id TEXT NOT NULL UNIQUE,
                migration_id TEXT NOT NULL,
                lifecycle_sequence INTEGER NOT NULL CHECK (lifecycle_sequence >= 1),
                before_state TEXT NOT NULL,
                after_state TEXT NOT NULL,
                verification_id TEXT NOT NULL,
                report_digest TEXT NOT NULL,
                previous_event_digest TEXT,
                event_digest TEXT NOT NULL UNIQUE,
                occurred_at TEXT NOT NULL,
                UNIQUE (migration_id, lifecycle_sequence),
                FOREIGN KEY (command_id)
                    REFERENCES governance_legacy_migration_lifecycle_commands(command_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (migration_id)
                    REFERENCES governance_legacy_migration_lifecycle_heads(migration_id)
                    ON DELETE RESTRICT
            ) WITHOUT ROWID
            """,
            """
            CREATE TABLE governance_legacy_migration_lifecycle_results (
                command_id TEXT PRIMARY KEY NOT NULL,
                migration_id TEXT NOT NULL,
                result_json TEXT NOT NULL,
                result_digest TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                FOREIGN KEY (command_id)
                    REFERENCES governance_legacy_migration_lifecycle_commands(command_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (migration_id)
                    REFERENCES governance_legacy_migration_lifecycle_heads(migration_id)
                    ON DELETE RESTRICT
            ) WITHOUT ROWID
            """,
            """
            CREATE TRIGGER governance_legacy_lifecycle_commands_insert_guard
            BEFORE INSERT ON governance_legacy_migration_lifecycle_commands
            WHEN NOT EXISTS (
                SELECT 1 FROM governance_legacy_migration_lifecycle_heads h
                WHERE h.migration_id = NEW.migration_id
                  AND h.project_namespace = NEW.project_namespace
                  AND h.project_id = NEW.project_id
                  AND h.state = 'staged_verified'
                  AND h.lifecycle_revision = NEW.expected_lifecycle_revision
            )
            BEGIN SELECT RAISE(ABORT, 'legacy lifecycle command root mismatch'); END
            """,
            """
            CREATE TRIGGER governance_legacy_lifecycle_events_insert_guard
            BEFORE INSERT ON governance_legacy_migration_lifecycle_events
            WHEN NOT EXISTS (
                SELECT 1
                FROM governance_legacy_migration_lifecycle_commands c
                JOIN governance_legacy_migration_lifecycle_heads h
                  ON h.migration_id = c.migration_id
                WHERE c.command_id = NEW.command_id
                  AND c.migration_id = NEW.migration_id
                  AND c.expected_lifecycle_revision = NEW.lifecycle_sequence
                  AND h.state = 'staged_verified'
                  AND h.lifecycle_revision = NEW.lifecycle_sequence
                  AND h.verification_id = NEW.verification_id
                  AND h.report_digest = NEW.report_digest
                  AND h.last_event_digest IS NEW.previous_event_digest
                  AND NEW.before_state = h.state
                  AND NEW.after_state = CASE c.action
                      WHEN 'activate' THEN 'activated'
                      WHEN 'rollback' THEN 'rolled_back'
                  END
                  AND NEW.occurred_at = c.occurred_at
            )
            BEGIN SELECT RAISE(ABORT, 'legacy lifecycle event root mismatch'); END
            """,
            """
            CREATE TRIGGER governance_legacy_lifecycle_results_insert_guard
            BEFORE INSERT ON governance_legacy_migration_lifecycle_results
            WHEN NOT EXISTS (
                SELECT 1
                FROM governance_legacy_migration_lifecycle_commands c
                JOIN governance_legacy_migration_lifecycle_events e
                  ON e.command_id = c.command_id
                WHERE c.command_id = NEW.command_id
                  AND c.migration_id = NEW.migration_id
                  AND NEW.created_at = c.occurred_at
            )
            BEGIN SELECT RAISE(ABORT, 'legacy lifecycle result root mismatch'); END
            """,
            """
            CREATE TRIGGER governance_legacy_lifecycle_head_commit_guard
            BEFORE UPDATE ON governance_legacy_migration_lifecycle_heads
            WHEN NEW.state IN ('activated', 'rolled_back')
              AND NOT EXISTS (
                SELECT 1
                FROM governance_legacy_migration_lifecycle_events e
                JOIN governance_legacy_migration_lifecycle_commands c
                  ON c.command_id = e.command_id
                JOIN governance_legacy_migration_lifecycle_results r
                  ON r.command_id = c.command_id
                WHERE e.migration_id = NEW.migration_id
                  AND e.lifecycle_sequence = OLD.lifecycle_revision
                  AND e.before_state = OLD.state
                  AND e.after_state = NEW.state
                  AND e.verification_id = OLD.verification_id
                  AND e.report_digest = OLD.report_digest
                  AND e.event_digest = NEW.last_event_digest
                  AND c.action = CASE NEW.state
                      WHEN 'activated' THEN 'activate'
                      WHEN 'rolled_back' THEN 'rollback'
                  END
            )
            BEGIN SELECT RAISE(ABORT, 'legacy lifecycle commit root mismatch'); END
            """,
            """
            CREATE TRIGGER governance_legacy_lifecycle_commands_no_update
            BEFORE UPDATE ON governance_legacy_migration_lifecycle_commands
            BEGIN SELECT RAISE(ABORT, 'legacy lifecycle command is immutable'); END
            """,
            """
            CREATE TRIGGER governance_legacy_lifecycle_commands_no_delete
            BEFORE DELETE ON governance_legacy_migration_lifecycle_commands
            BEGIN SELECT RAISE(ABORT, 'legacy lifecycle command is durable'); END
            """,
            """
            CREATE TRIGGER governance_legacy_lifecycle_events_no_update
            BEFORE UPDATE ON governance_legacy_migration_lifecycle_events
            BEGIN SELECT RAISE(ABORT, 'legacy lifecycle event is immutable'); END
            """,
            """
            CREATE TRIGGER governance_legacy_lifecycle_events_no_delete
            BEFORE DELETE ON governance_legacy_migration_lifecycle_events
            BEGIN SELECT RAISE(ABORT, 'legacy lifecycle event is durable'); END
            """,
            """
            CREATE TRIGGER governance_legacy_lifecycle_results_no_update
            BEFORE UPDATE ON governance_legacy_migration_lifecycle_results
            BEGIN SELECT RAISE(ABORT, 'legacy lifecycle result is immutable'); END
            """,
            """
            CREATE TRIGGER governance_legacy_lifecycle_results_no_delete
            BEFORE DELETE ON governance_legacy_migration_lifecycle_results
            BEGIN SELECT RAISE(ABORT, 'legacy lifecycle result is durable'); END
            """,
        ),
    ),
    Migration(
        version=25,
        name="legacy-lifecycle-compatibility-hardening",
        statements=(
            "DROP TRIGGER governance_legacy_lifecycle_results_insert_guard",
            """
            CREATE TRIGGER governance_legacy_lifecycle_results_insert_guard
            BEFORE INSERT ON governance_legacy_migration_lifecycle_results
            WHEN json_valid(NEW.result_json) != 1
              OR json_extract(NEW.result_json, '$.command_id') IS NOT NEW.command_id
              OR json_extract(NEW.result_json, '$.migration_id') IS NOT NEW.migration_id
              OR json_extract(NEW.result_json, '$.state') IS NOT 'activated'
              OR json_extract(NEW.result_json, '$.result_digest') IS NOT NEW.result_digest
              OR julianday(json_extract(NEW.result_json, '$.activated_at'))
                    IS NOT julianday(NEW.created_at)
              OR json_extract(NEW.result_json, '$.replayed') != 0
              OR NOT EXISTS (
                SELECT 1
                FROM governance_legacy_migration_lifecycle_commands c
                JOIN governance_legacy_migration_lifecycle_events e
                  ON e.command_id = c.command_id
                WHERE c.command_id = NEW.command_id
                  AND c.migration_id = NEW.migration_id
                  AND NEW.created_at = c.occurred_at
                  AND json_extract(NEW.result_json, '$.event_id') = e.event_id
                  AND json_extract(NEW.result_json, '$.project_ref.namespace') =
                      c.project_namespace
                  AND json_extract(NEW.result_json, '$.project_ref.project_id') = c.project_id
                  AND json_extract(NEW.result_json, '$.lifecycle_revision') =
                      c.expected_lifecycle_revision + 1
                  AND json_extract(NEW.result_json, '$.verification_id') = e.verification_id
                  AND json_extract(NEW.result_json, '$.report_digest') = e.report_digest
                  AND json_extract(NEW.result_json, '$.actor_ref.actor_id') = c.actor_id
                  AND json_extract(NEW.result_json, '$.actor_ref.actor_type') = c.actor_type
            )
            BEGIN SELECT RAISE(ABORT, 'legacy lifecycle result root mismatch'); END
            """,
            "DROP TRIGGER governance_legacy_migration_lifecycle_heads_update_guard",
            """
            UPDATE governance_legacy_migration_lifecycle_heads AS h
            SET state = 'recovery_hold',
                lifecycle_revision = lifecycle_revision + 1,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE h.state = 'staged_verified'
              AND (
                EXISTS (
                    SELECT 1
                    FROM governance_legacy_migration_items i
                    JOIN governance_active_proposals p
                      ON p.project_namespace = i.project_namespace
                     AND p.project_id = i.project_id
                     AND p.proposal_id = i.proposal_id
                    WHERE i.migration_id = h.migration_id
                      AND NOT (
                          (
                              p.active_definition_digest = i.definition_digest
                              AND p.content_revision = i.content_revision
                              AND p.state_revision = i.state_revision
                              AND p.decision_epoch = i.decision_epoch
                              AND p.status = CASE i.target_status
                                  WHEN 'reviewed' THEN 'reviewed'
                                  ELSE 'draft'
                              END
                          )
                          OR EXISTS (
                              SELECT 1
                              FROM governance_legacy_approval_reviews r
                              WHERE r.migration_id = i.migration_id
                                AND r.project_namespace = i.project_namespace
                                AND r.project_id = i.project_id
                                AND r.proposal_id = i.proposal_id
                                AND p.active_definition_digest = i.definition_digest
                                AND p.content_revision = i.content_revision
                                AND p.state_revision = r.source_state_revision
                                AND p.decision_epoch = i.decision_epoch + 1
                                AND p.status = 'draft'
                          )
                      )
                )
                OR EXISTS (
                    SELECT 1
                    FROM governance_legacy_migration_items i
                    JOIN governance_legacy_import_commands c
                      ON c.migration_id = i.migration_id
                     AND c.project_namespace = i.project_namespace
                     AND c.project_id = i.project_id
                     AND c.proposal_id = i.proposal_id
                    JOIN governance_audit_events a
                      ON a.project_namespace = i.project_namespace
                     AND a.project_id = i.project_id
                     AND a.proposal_id = i.proposal_id
                    WHERE i.migration_id = h.migration_id
                      AND a.command_id != c.command_id
                      AND NOT EXISTS (
                          SELECT 1
                          FROM governance_legacy_approval_reviews r
                          WHERE r.migration_id = i.migration_id
                            AND r.project_namespace = i.project_namespace
                            AND r.project_id = i.project_id
                            AND r.proposal_id = i.proposal_id
                            AND r.review_id = a.command_id
                      )
                )
              )
            """,
            """
            CREATE TRIGGER governance_legacy_migration_lifecycle_heads_update_guard
            BEFORE UPDATE ON governance_legacy_migration_lifecycle_heads
            WHEN OLD.migration_id != NEW.migration_id
              OR OLD.project_namespace != NEW.project_namespace
              OR OLD.project_id != NEW.project_id
              OR NEW.lifecycle_revision != OLD.lifecycle_revision + 1
              OR OLD.state IN ('activated', 'rolled_back', 'recovery_hold')
              OR NOT (
                    (OLD.state = 'imported' AND NEW.state = 'staged_verified'
                     AND OLD.verification_id IS NULL AND NEW.verification_id IS NOT NULL
                     AND OLD.report_digest IS NULL AND NEW.report_digest IS NOT NULL
                     AND NEW.last_event_digest IS NULL)
                    OR
                    (OLD.state = 'staged_verified'
                     AND NEW.state IN ('activated', 'rolled_back')
                     AND OLD.verification_id IS NEW.verification_id
                     AND OLD.report_digest IS NEW.report_digest
                     AND NEW.last_event_digest IS NOT NULL)
                )
            BEGIN SELECT RAISE(ABORT, 'legacy lifecycle transition is invalid'); END
            """,
        ),
    ),
    Migration(
        version=26,
        name="legacy-rollback-destination-provenance",
        statements=(
            """
            CREATE TABLE governance_legacy_import_destination_roots (
                migration_id TEXT NOT NULL,
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                destination_ref TEXT NOT NULL,
                existed_before INTEGER NOT NULL CHECK (existed_before IN (0, 1)),
                previous_next_sequence INTEGER,
                previous_delivered_sequence INTEGER,
                previous_operator_hold INTEGER,
                previous_updated_at TEXT,
                captured_at TEXT NOT NULL,
                PRIMARY KEY (migration_id, project_namespace, project_id, proposal_id),
                FOREIGN KEY (migration_id, project_namespace, project_id, proposal_id)
                    REFERENCES governance_legacy_migration_items(
                        migration_id, project_namespace, project_id, proposal_id
                    ) ON DELETE RESTRICT,
                CHECK (
                    (
                        existed_before = 0
                        AND previous_next_sequence IS NULL
                        AND previous_delivered_sequence IS NULL
                        AND previous_operator_hold IS NULL
                        AND previous_updated_at IS NULL
                    )
                    OR
                    (
                        existed_before = 1
                        AND previous_next_sequence >= 1
                        AND previous_delivered_sequence >= 0
                        AND previous_delivered_sequence < previous_next_sequence
                        AND previous_operator_hold IN (0, 1)
                        AND previous_updated_at IS NOT NULL
                    )
                )
            ) WITHOUT ROWID
            """,
            """
            CREATE TRIGGER governance_legacy_import_destination_roots_no_update
            BEFORE UPDATE ON governance_legacy_import_destination_roots
            BEGIN SELECT RAISE(ABORT, 'legacy destination provenance is immutable'); END
            """,
            """
            CREATE TRIGGER governance_legacy_import_destination_roots_no_delete
            BEFORE DELETE ON governance_legacy_import_destination_roots
            BEGIN SELECT RAISE(ABORT, 'legacy destination provenance is durable'); END
            """,
        ),
    ),
)


class MigrationRunner:
    def __init__(self, migrations: tuple[Migration, ...] = INITIAL_MIGRATIONS) -> None:
        versions = tuple(migration.version for migration in migrations)
        if versions != tuple(range(1, len(migrations) + 1)):
            raise GovernanceMigrationError("migration version은 1부터 연속이어야 합니다.")
        self.migrations = migrations

    @property
    def latest_version(self) -> int:
        return self.migrations[-1].version if self.migrations else 0

    @staticmethod
    def _ensure_history_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS governance_schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _history_table_exists(connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'governance_schema_migrations'
            """
        ).fetchone()
        return row is not None

    def current_version(self, connection: sqlite3.Connection) -> int:
        return self.verify(connection)

    def _applied_rows(self, connection: sqlite3.Connection) -> list[tuple[int, str, str]]:
        if not self._history_table_exists(connection):
            raise GovernanceMigrationError("migration history table이 없습니다.")
        return cast(
            list[tuple[int, str, str]],
            connection.execute(
                """
                SELECT version, name, checksum
                FROM governance_schema_migrations
                ORDER BY version
                """
            ).fetchall(),
        )

    def _verify_rows(self, applied_rows: list[tuple[int, str, str]]) -> int:
        known = {migration.version: migration for migration in self.migrations}
        for version, _name, _checksum in applied_rows:
            if version not in known:
                raise GovernanceMigrationError(
                    f"지원하지 않는 future schema version입니다: {version}"
                )
        applied_versions = tuple(row[0] for row in applied_rows)
        if applied_versions != tuple(range(1, len(applied_versions) + 1)):
            raise GovernanceMigrationError(
                f"migration history version이 연속적이지 않습니다: {applied_versions}"
            )
        for version_value, name_value, checksum_value in applied_rows:
            version = version_value
            migration = known[version]
            if str(name_value) != migration.name or str(checksum_value) != migration.checksum:
                raise GovernanceMigrationError(
                    f"migration history가 현재 contract와 다릅니다: version={version}"
                )
        return applied_versions[-1] if applied_versions else 0

    def verify(self, connection: sqlite3.Connection) -> int:
        return self._verify_rows(self._applied_rows(connection))

    def verify_schema(self, connection: sqlite3.Connection, schema_version: int) -> None:
        expected_columns = {
            "governance_schema_migrations": (
                ("version", "INTEGER", 0, 1),
                ("name", "TEXT", 1, 0),
                ("checksum", "TEXT", 1, 0),
                ("applied_at", "TEXT", 1, 0),
            ),
            "governance_store_metadata": (
                ("key", "TEXT", 1, 1),
                ("value", "TEXT", 1, 0),
            ),
        }
        if schema_version >= 2:
            expected_columns.update(
                {
                    "governance_active_proposals": (
                        ("project_namespace", "TEXT", 1, 1),
                        ("project_id", "TEXT", 1, 2),
                        ("proposal_id", "TEXT", 1, 3),
                        ("active_definition_digest", "TEXT", 1, 0),
                        ("content_revision", "INTEGER", 1, 0),
                        ("state_revision", "INTEGER", 1, 0),
                        ("decision_epoch", "INTEGER", 1, 0),
                        ("status", "TEXT", 1, 0),
                        ("created_at", "TEXT", 1, 0),
                        ("updated_at", "TEXT", 1, 0),
                    ),
                    "governance_definition_revisions": (
                        ("project_namespace", "TEXT", 1, 1),
                        ("project_id", "TEXT", 1, 2),
                        ("proposal_id", "TEXT", 1, 3),
                        ("content_revision", "INTEGER", 1, 4),
                        ("definition_digest", "TEXT", 1, 0),
                        ("previous_definition_digest", "TEXT", 0, 0),
                        ("activated_from_status", "TEXT", 0, 0),
                        ("activated_at", "TEXT", 1, 0),
                    ),
                }
            )
        if schema_version >= 3:
            expected_columns.update(
                {
                    "governance_action_tokens": (
                        ("token_id", "TEXT", 1, 1),
                        ("token_hash", "TEXT", 1, 0),
                        ("project_namespace", "TEXT", 1, 0),
                        ("project_id", "TEXT", 1, 0),
                        ("proposal_id", "TEXT", 1, 0),
                        ("active_definition_digest", "TEXT", 1, 0),
                        ("content_revision", "INTEGER", 1, 0),
                        ("state_revision", "INTEGER", 1, 0),
                        ("decision_epoch", "INTEGER", 1, 0),
                        ("allowed_action", "TEXT", 1, 0),
                        ("allowed_actor_id", "TEXT", 1, 0),
                        ("allowed_actor_type", "TEXT", 1, 0),
                        ("bound_channel_json", "TEXT", 1, 0),
                        ("issued_at", "TEXT", 1, 0),
                        ("expires_at", "TEXT", 1, 0),
                        ("state", "TEXT", 1, 0),
                        ("resolved_at", "TEXT", 0, 0),
                    ),
                    "governance_decision_results": (
                        ("idempotency_key", "TEXT", 1, 1),
                        ("request_fingerprint", "TEXT", 1, 0),
                        ("project_namespace", "TEXT", 1, 0),
                        ("project_id", "TEXT", 1, 0),
                        ("proposal_id", "TEXT", 1, 0),
                        ("action", "TEXT", 1, 0),
                        ("actor_id", "TEXT", 1, 0),
                        ("actor_type", "TEXT", 1, 0),
                        ("channel_json", "TEXT", 1, 0),
                        ("proposal_status", "TEXT", 1, 0),
                        ("active_definition_digest", "TEXT", 1, 0),
                        ("content_revision", "INTEGER", 1, 0),
                        ("state_revision", "INTEGER", 1, 0),
                        ("decision_epoch", "INTEGER", 1, 0),
                        ("token_id", "TEXT", 1, 0),
                        ("processed_at", "TEXT", 1, 0),
                    ),
                }
            )
        if schema_version >= 4:
            expected_columns.update(
                {
                    "governance_ingress_commands": (
                        ("command_id", "TEXT", 1, 1),
                        ("provider", "TEXT", 1, 0),
                        ("provider_installation_ref", "TEXT", 1, 0),
                        ("provider_fingerprint", "TEXT", 1, 0),
                        ("raw_body_digest", "TEXT", 1, 0),
                        ("external_event_id", "TEXT", 1, 0),
                        ("external_actor_key", "TEXT", 1, 0),
                        ("channel_json", "TEXT", 1, 0),
                        ("credential_kind", "TEXT", 1, 0),
                        ("credential_id", "TEXT", 1, 0),
                        ("credential_hash", "TEXT", 1, 0),
                        ("action", "TEXT", 1, 0),
                        ("received_at", "TEXT", 1, 0),
                        ("state", "TEXT", 1, 0),
                        ("attempts", "INTEGER", 1, 0),
                        ("claim_generation", "INTEGER", 1, 0),
                        ("lease_owner", "TEXT", 0, 0),
                        ("lease_expires_at", "TEXT", 0, 0),
                        ("retry_at", "TEXT", 0, 0),
                        ("completed_at", "TEXT", 0, 0),
                        ("last_error_code", "TEXT", 0, 0),
                    ),
                }
            )
        if schema_version >= 5:
            expected_columns.update(
                {
                    "governance_actors": (
                        ("actor_id", "TEXT", 1, 1),
                        ("actor_type", "TEXT", 1, 0),
                        ("actor_profile", "TEXT", 1, 0),
                        ("status", "TEXT", 1, 0),
                        ("created_at", "TEXT", 1, 0),
                        ("updated_at", "TEXT", 1, 0),
                    ),
                    "governance_external_actor_bindings": (
                        ("binding_id", "TEXT", 1, 1),
                        ("provider", "TEXT", 1, 0),
                        ("provider_installation_ref", "TEXT", 1, 0),
                        ("external_actor_id", "TEXT", 1, 0),
                        ("actor_id", "TEXT", 1, 0),
                        ("binding_version", "INTEGER", 1, 0),
                        ("status", "TEXT", 1, 0),
                        ("created_at", "TEXT", 1, 0),
                        ("disabled_at", "TEXT", 0, 0),
                    ),
                    "governance_actor_permissions": (
                        ("actor_id", "TEXT", 1, 1),
                        ("project_namespace", "TEXT", 1, 2),
                        ("project_id", "TEXT", 1, 3),
                        ("permission", "TEXT", 1, 4),
                        ("granted_at", "TEXT", 1, 0),
                        ("granted_by", "TEXT", 1, 0),
                    ),
                    "governance_binding_transitions": (
                        ("transition_id", "TEXT", 1, 1),
                        ("transition_type", "TEXT", 1, 0),
                        ("provider", "TEXT", 1, 0),
                        ("provider_installation_ref", "TEXT", 1, 0),
                        ("external_actor_id", "TEXT", 1, 0),
                        ("before_binding_id", "TEXT", 0, 0),
                        ("after_binding_id", "TEXT", 0, 0),
                        ("before_actor_id", "TEXT", 0, 0),
                        ("after_actor_id", "TEXT", 0, 0),
                        ("approval_id", "TEXT", 1, 0),
                        ("approved_by", "TEXT", 1, 0),
                        ("reason", "TEXT", 1, 0),
                        ("occurred_at", "TEXT", 1, 0),
                    ),
                }
            )
        if schema_version >= 6:
            expected_columns.update(
                {
                    "governance_aggregate_sequences": (
                        ("project_namespace", "TEXT", 1, 1),
                        ("project_id", "TEXT", 1, 2),
                        ("proposal_id", "TEXT", 1, 3),
                        ("aggregate_sequence", "INTEGER", 1, 0),
                        ("last_event_hash", "TEXT", 0, 0),
                    ),
                    "governance_audit_events": (
                        ("event_id", "TEXT", 1, 1),
                        ("command_id", "TEXT", 1, 0),
                        ("event_type", "TEXT", 1, 0),
                        ("project_namespace", "TEXT", 1, 0),
                        ("project_id", "TEXT", 1, 0),
                        ("proposal_id", "TEXT", 1, 0),
                        ("aggregate_sequence", "INTEGER", 1, 0),
                        ("actor_id", "TEXT", 1, 0),
                        ("actor_type", "TEXT", 1, 0),
                        ("policy_snapshot_id", "TEXT", 1, 0),
                        ("before_state", "TEXT", 1, 0),
                        ("after_state", "TEXT", 1, 0),
                        ("definition_digest", "TEXT", 1, 0),
                        ("previous_event_hash", "TEXT", 0, 0),
                        ("event_hash", "TEXT", 1, 0),
                        ("occurred_at", "TEXT", 1, 0),
                    ),
                    "governance_outbox_destinations": (
                        ("destination_ref", "TEXT", 1, 1),
                        ("next_sequence", "INTEGER", 1, 0),
                        ("delivered_sequence", "INTEGER", 1, 0),
                        ("operator_hold", "INTEGER", 1, 0),
                        ("updated_at", "TEXT", 1, 0),
                    ),
                    "governance_outbox_events": (
                        ("event_id", "TEXT", 1, 1),
                        ("project_namespace", "TEXT", 1, 0),
                        ("project_id", "TEXT", 1, 0),
                        ("proposal_id", "TEXT", 1, 0),
                        ("aggregate_sequence", "INTEGER", 1, 0),
                        ("destination_ref", "TEXT", 1, 0),
                        ("destination_sequence", "INTEGER", 1, 0),
                        ("source_state_revision", "INTEGER", 1, 0),
                        ("supersession_key", "TEXT", 0, 0),
                        ("payload_digest", "TEXT", 1, 0),
                        ("payload_json", "TEXT", 1, 0),
                        ("state", "TEXT", 1, 0),
                        ("attempts", "INTEGER", 1, 0),
                        ("claim_generation", "INTEGER", 1, 0),
                        ("lease_owner", "TEXT", 0, 0),
                        ("lease_expires_at", "TEXT", 0, 0),
                        ("retry_at", "TEXT", 0, 0),
                        ("delivered_at", "TEXT", 0, 0),
                        ("remote_receipt", "TEXT", 0, 0),
                        ("last_error_code", "TEXT", 0, 0),
                        ("created_at", "TEXT", 1, 0),
                    ),
                    "governance_outbox_dead_letters": (
                        ("dead_letter_id", "TEXT", 1, 1),
                        ("event_id", "TEXT", 1, 0),
                        ("destination_ref", "TEXT", 1, 0),
                        ("destination_sequence", "INTEGER", 1, 0),
                        ("attempts", "INTEGER", 1, 0),
                        ("error_code", "TEXT", 1, 0),
                        ("created_at", "TEXT", 1, 0),
                    ),
                    "governance_operator_holds": (
                        ("hold_id", "TEXT", 1, 1),
                        ("scope_kind", "TEXT", 1, 0),
                        ("scope_ref", "TEXT", 1, 0),
                        ("reason_code", "TEXT", 1, 0),
                        ("source_event_id", "TEXT", 1, 0),
                        ("created_at", "TEXT", 1, 0),
                        ("resolved_at", "TEXT", 0, 0),
                    ),
                }
            )
        if schema_version >= 7:
            expected_columns["governance_audit_events"] = (
                *expected_columns["governance_audit_events"],
                ("destination_manifest_digest", "TEXT", 0, 0),
                ("destination_count", "INTEGER", 0, 0),
            )
        if schema_version >= 9:
            expected_columns.update(
                {
                    "governance_approved_snapshots": (
                        ("snapshot_id", "TEXT", 1, 1),
                        ("project_namespace", "TEXT", 1, 0),
                        ("project_id", "TEXT", 1, 0),
                        ("proposal_id", "TEXT", 1, 0),
                        ("definition_digest", "TEXT", 1, 0),
                        ("snapshot_digest", "TEXT", 1, 0),
                        ("content_revision", "INTEGER", 1, 0),
                        ("state_revision", "INTEGER", 1, 0),
                        ("decision_epoch", "INTEGER", 1, 0),
                        ("expected_base_revision", "TEXT", 1, 0),
                        ("approved_at", "TEXT", 1, 0),
                    ),
                    "governance_apply_grants": (
                        ("grant_id", "TEXT", 1, 1),
                        ("grant_hash", "TEXT", 1, 0),
                        ("snapshot_id", "TEXT", 1, 0),
                        ("project_namespace", "TEXT", 1, 0),
                        ("project_id", "TEXT", 1, 0),
                        ("proposal_id", "TEXT", 1, 0),
                        ("approved_snapshot_digest", "TEXT", 1, 0),
                        ("content_revision", "INTEGER", 1, 0),
                        ("state_revision", "INTEGER", 1, 0),
                        ("decision_epoch", "INTEGER", 1, 0),
                        ("allowed_action", "TEXT", 1, 0),
                        ("allowed_actor_id", "TEXT", 1, 0),
                        ("allowed_actor_type", "TEXT", 1, 0),
                        ("bound_channel_json", "TEXT", 1, 0),
                        ("issued_at", "TEXT", 1, 0),
                        ("expires_at", "TEXT", 1, 0),
                        ("state", "TEXT", 1, 0),
                        ("resolved_at", "TEXT", 0, 0),
                    ),
                    "governance_apply_jobs": (
                        ("job_id", "TEXT", 1, 1),
                        ("snapshot_id", "TEXT", 1, 0),
                        ("project_namespace", "TEXT", 1, 0),
                        ("project_id", "TEXT", 1, 0),
                        ("proposal_id", "TEXT", 1, 0),
                        ("approved_snapshot_digest", "TEXT", 1, 0),
                        ("expected_base_revision", "TEXT", 1, 0),
                        ("status", "TEXT", 1, 0),
                        ("attempts", "INTEGER", 1, 0),
                        ("fencing_token", "INTEGER", 1, 0),
                        ("lease_owner", "TEXT", 0, 0),
                        ("lease_expires_at", "TEXT", 0, 0),
                        ("retry_at", "TEXT", 0, 0),
                        ("staged_artifact_digest", "TEXT", 0, 0),
                        ("publish_request_digest", "TEXT", 0, 0),
                        ("last_error_code", "TEXT", 0, 0),
                        ("created_at", "TEXT", 1, 0),
                        ("updated_at", "TEXT", 1, 0),
                    ),
                    "governance_apply_request_results": (
                        ("idempotency_key", "TEXT", 1, 1),
                        ("request_fingerprint", "TEXT", 1, 0),
                        ("project_namespace", "TEXT", 1, 0),
                        ("project_id", "TEXT", 1, 0),
                        ("proposal_id", "TEXT", 1, 0),
                        ("snapshot_id", "TEXT", 1, 0),
                        ("job_id", "TEXT", 1, 0),
                        ("actor_id", "TEXT", 1, 0),
                        ("actor_type", "TEXT", 1, 0),
                        ("channel_json", "TEXT", 1, 0),
                        ("proposal_status", "TEXT", 1, 0),
                        ("processed_at", "TEXT", 1, 0),
                    ),
                }
            )
        if schema_version >= 10:
            apply_result_columns = expected_columns["governance_apply_request_results"]
            expected_columns["governance_apply_request_results"] = (
                *apply_result_columns[:6],
                ("grant_id", "TEXT", 1, 0),
                *apply_result_columns[6:],
            )
        if schema_version >= 11:
            expected_columns.update(
                {
                    "governance_apply_job_events": (
                        ("job_event_id", "TEXT", 1, 1),
                        ("command_id", "TEXT", 1, 0),
                        ("job_id", "TEXT", 1, 0),
                        ("event_sequence", "INTEGER", 1, 0),
                        ("snapshot_id", "TEXT", 1, 0),
                        ("project_namespace", "TEXT", 1, 0),
                        ("project_id", "TEXT", 1, 0),
                        ("proposal_id", "TEXT", 1, 0),
                        ("event_type", "TEXT", 1, 0),
                        ("worker_id", "TEXT", 1, 0),
                        ("before_status", "TEXT", 1, 0),
                        ("status", "TEXT", 1, 0),
                        ("attempts", "INTEGER", 1, 0),
                        ("fencing_token", "INTEGER", 1, 0),
                        ("lease_owner", "TEXT", 0, 0),
                        ("lease_expires_at", "TEXT", 0, 0),
                        ("retry_at", "TEXT", 0, 0),
                        ("staged_artifact_digest", "TEXT", 0, 0),
                        ("publish_request_digest", "TEXT", 0, 0),
                        ("last_error_code", "TEXT", 0, 0),
                        ("payload_digest", "TEXT", 1, 0),
                        ("payload_json", "TEXT", 1, 0),
                        ("created_at", "TEXT", 1, 0),
                    ),
                    "governance_staging_artifacts": (
                        ("job_id", "TEXT", 1, 1),
                        ("fencing_token", "INTEGER", 1, 2),
                        ("artifact_digest", "TEXT", 1, 0),
                        ("artifact_bytes", "BLOB", 1, 0),
                        ("created_at", "TEXT", 1, 0),
                    ),
                    "governance_publish_inputs": (
                        ("job_id", "TEXT", 1, 1),
                        ("fencing_token", "INTEGER", 1, 2),
                        ("publish_request_digest", "TEXT", 1, 0),
                        ("publish_request_bytes", "BLOB", 1, 0),
                        ("created_at", "TEXT", 1, 0),
                    ),
                }
            )
        if schema_version >= 12:
            expected_columns.update(
                {
                    "governance_publish_intents": (
                        ("intent_id", "TEXT", 1, 1),
                        ("job_id", "TEXT", 1, 0),
                        ("snapshot_id", "TEXT", 1, 0),
                        ("project_namespace", "TEXT", 1, 0),
                        ("project_id", "TEXT", 1, 0),
                        ("proposal_id", "TEXT", 1, 0),
                        ("fencing_token", "INTEGER", 1, 0),
                        ("approved_snapshot_digest", "TEXT", 1, 0),
                        ("expected_base_revision", "TEXT", 1, 0),
                        ("staged_artifact_digest", "TEXT", 1, 0),
                        ("publish_request_digest", "TEXT", 1, 0),
                        ("canonical_ref", "TEXT", 1, 0),
                        ("expected_old_ref", "TEXT", 1, 0),
                        ("candidate_commit", "TEXT", 1, 0),
                        ("candidate_tree_digest", "TEXT", 1, 0),
                        ("status", "TEXT", 1, 0),
                        ("prepared_at", "TEXT", 1, 0),
                        ("resolved_at", "TEXT", 0, 0),
                        ("last_error_code", "TEXT", 0, 0),
                    ),
                    "governance_project_publish_gates": (
                        ("project_namespace", "TEXT", 1, 1),
                        ("project_id", "TEXT", 1, 2),
                        ("canonical_ref", "TEXT", 1, 0),
                        ("active_intent_id", "TEXT", 0, 0),
                        ("gate_revision", "INTEGER", 1, 0),
                        ("state", "TEXT", 1, 0),
                        ("updated_at", "TEXT", 1, 0),
                    ),
                    "governance_publish_results": (
                        ("intent_id", "TEXT", 1, 1),
                        ("job_id", "TEXT", 1, 0),
                        ("snapshot_id", "TEXT", 1, 0),
                        ("project_namespace", "TEXT", 1, 0),
                        ("project_id", "TEXT", 1, 0),
                        ("proposal_id", "TEXT", 1, 0),
                        ("fencing_token", "INTEGER", 1, 0),
                        ("expected_old_ref", "TEXT", 1, 0),
                        ("candidate_commit", "TEXT", 1, 0),
                        ("actual_ref", "TEXT", 1, 0),
                        ("outcome", "TEXT", 1, 0),
                        ("error_code", "TEXT", 0, 0),
                        ("resolved_at", "TEXT", 1, 0),
                    ),
                }
            )
        if schema_version >= 13:
            expected_columns["governance_publish_claims"] = (
                ("claim_id", "TEXT", 1, 1),
                ("intent_id", "TEXT", 1, 0),
                ("project_namespace", "TEXT", 1, 0),
                ("project_id", "TEXT", 1, 0),
                ("coordinator_id", "TEXT", 1, 0),
                ("claim_fencing_token", "INTEGER", 1, 0),
                ("state", "TEXT", 1, 0),
                ("claimed_at", "TEXT", 1, 0),
                ("resolved_at", "TEXT", 0, 0),
            )
        if schema_version >= 14:
            expected_columns["governance_active_proposals"] = (
                *expected_columns["governance_active_proposals"],
                ("applied_revision", "TEXT", 0, 0),
            )
            expected_columns["governance_publish_resolution_events"] = (
                ("resolution_event_id", "TEXT", 1, 1),
                ("command_id", "TEXT", 1, 0),
                ("intent_id", "TEXT", 1, 0),
                ("resolution_sequence", "INTEGER", 1, 0),
                ("claim_id", "TEXT", 1, 0),
                ("resolver_id", "TEXT", 1, 0),
                ("project_namespace", "TEXT", 1, 0),
                ("project_id", "TEXT", 1, 0),
                ("proposal_id", "TEXT", 1, 0),
                ("job_id", "TEXT", 1, 0),
                ("resolution_type", "TEXT", 1, 0),
                ("actual_ref", "TEXT", 0, 0),
                ("intent_status", "TEXT", 1, 0),
                ("job_status", "TEXT", 1, 0),
                ("proposal_status", "TEXT", 1, 0),
                ("proposal_state_revision", "INTEGER", 1, 0),
                ("applied_revision", "TEXT", 0, 0),
                ("error_code", "TEXT", 0, 0),
                ("payload_digest", "TEXT", 1, 0),
                ("payload_json", "TEXT", 1, 0),
                ("created_at", "TEXT", 1, 0),
            )
        if schema_version >= 15:
            expected_columns["governance_publish_resolution_roots"] = (
                ("resolution_event_id", "TEXT", 1, 1),
                ("intent_id", "TEXT", 1, 0),
                ("claim_id", "TEXT", 1, 0),
                ("job_id", "TEXT", 1, 0),
                ("project_namespace", "TEXT", 1, 0),
                ("project_id", "TEXT", 1, 0),
                ("proposal_id", "TEXT", 1, 0),
                ("before_job_status", "TEXT", 1, 0),
                ("stream_revision", "INTEGER", 1, 0),
            )
        if schema_version >= 18:
            expected_columns.update(
                {
                    "governance_legacy_migrations": (
                        ("migration_id", "TEXT", 1, 1),
                        ("project_namespace", "TEXT", 1, 0),
                        ("project_id", "TEXT", 1, 0),
                        ("freeze_id", "TEXT", 1, 0),
                        ("snapshot_id", "TEXT", 1, 0),
                        ("snapshot_digest", "TEXT", 1, 0),
                        ("plan_digest", "TEXT", 1, 0),
                        ("mapping_policy_version", "INTEGER", 1, 0),
                        ("base_revision", "TEXT", 1, 0),
                        ("validation_policy_ref", "TEXT", 1, 0),
                        ("project_pack_backup_path", "TEXT", 1, 0),
                        ("project_pack_backup_digest", "TEXT", 1, 0),
                        ("governance_backup_path", "TEXT", 1, 0),
                        ("governance_backup_digest", "TEXT", 1, 0),
                        ("proposal_count", "INTEGER", 1, 0),
                        ("status", "TEXT", 1, 0),
                        ("prepared_at", "TEXT", 1, 0),
                        ("state_imported_at", "TEXT", 0, 0),
                    ),
                    "governance_legacy_migration_items": (
                        ("migration_id", "TEXT", 1, 1),
                        ("project_namespace", "TEXT", 1, 2),
                        ("project_id", "TEXT", 1, 3),
                        ("proposal_id", "TEXT", 1, 4),
                        ("source_status", "TEXT", 1, 0),
                        ("source_revision", "INTEGER", 1, 0),
                        ("target_status", "TEXT", 1, 0),
                        ("definition_digest", "TEXT", 1, 0),
                        ("proposal_artifact_digest", "TEXT", 1, 0),
                        ("content_revision", "INTEGER", 1, 0),
                        ("state_revision", "INTEGER", 1, 0),
                        ("decision_epoch", "INTEGER", 1, 0),
                        ("approval_disposition", "TEXT", 1, 0),
                        ("imported_at", "TEXT", 1, 0),
                    ),
                }
            )
        if schema_version >= 19:
            items = expected_columns["governance_legacy_migration_items"]
            expected_columns["governance_legacy_migration_items"] = (
                *items[:-1],
                ("legacy_git_revision", "TEXT", 0, 0),
                items[-1],
            )
        if schema_version >= 20:
            expected_columns.update(
                {
                    "governance_legacy_import_commands": (
                        ("command_id", "TEXT", 1, 1),
                        ("migration_id", "TEXT", 1, 0),
                        ("project_namespace", "TEXT", 1, 0),
                        ("project_id", "TEXT", 1, 0),
                        ("proposal_id", "TEXT", 1, 0),
                        ("idempotency_key", "TEXT", 1, 0),
                        ("request_fingerprint", "TEXT", 1, 0),
                        ("event_type", "TEXT", 1, 0),
                        ("actor_id", "TEXT", 1, 0),
                        ("actor_type", "TEXT", 1, 0),
                        ("occurred_at", "TEXT", 1, 0),
                        ("source_artifact_digest", "TEXT", 1, 0),
                        ("reason", "TEXT", 0, 0),
                        ("definition_digest", "TEXT", 1, 0),
                        ("source_state_revision", "INTEGER", 1, 0),
                        ("payload_digest", "TEXT", 1, 0),
                        ("payload_json", "TEXT", 1, 0),
                    ),
                    "governance_legacy_approval_holds": (
                        ("migration_id", "TEXT", 1, 0),
                        ("project_namespace", "TEXT", 1, 1),
                        ("project_id", "TEXT", 1, 2),
                        ("proposal_id", "TEXT", 1, 3),
                        ("reason_code", "TEXT", 1, 0),
                        ("source_artifact_digest", "TEXT", 1, 0),
                        ("created_at", "TEXT", 1, 0),
                    ),
                    "governance_legacy_event_backfill_pending": (
                        ("migration_id", "TEXT", 1, 1),
                        ("project_namespace", "TEXT", 1, 2),
                        ("project_id", "TEXT", 1, 3),
                        ("proposal_id", "TEXT", 1, 4),
                        ("created_at", "TEXT", 1, 0),
                    ),
                    "governance_legacy_approval_reviews": (
                        ("review_id", "TEXT", 1, 1),
                        ("migration_id", "TEXT", 1, 0),
                        ("project_namespace", "TEXT", 1, 0),
                        ("project_id", "TEXT", 1, 0),
                        ("proposal_id", "TEXT", 1, 0),
                        ("idempotency_key", "TEXT", 1, 0),
                        ("request_fingerprint", "TEXT", 1, 0),
                        ("actor_id", "TEXT", 1, 0),
                        ("actor_type", "TEXT", 1, 0),
                        ("occurred_at", "TEXT", 1, 0),
                        ("reason", "TEXT", 1, 0),
                        ("request_id", "TEXT", 1, 0),
                        ("channel_json", "TEXT", 1, 0),
                        ("definition_digest", "TEXT", 1, 0),
                        ("source_state_revision", "INTEGER", 1, 0),
                        ("payload_digest", "TEXT", 1, 0),
                        ("payload_json", "TEXT", 1, 0),
                    ),
                }
            )
        if schema_version >= 21:
            expected_columns["governance_legacy_migration_verifications"] = (
                ("verification_id", "TEXT", 1, 1),
                ("migration_id", "TEXT", 1, 0),
                ("project_namespace", "TEXT", 1, 0),
                ("project_id", "TEXT", 1, 0),
                ("snapshot_id", "TEXT", 1, 0),
                ("snapshot_digest", "TEXT", 1, 0),
                ("plan_digest", "TEXT", 1, 0),
                ("proposal_count", "INTEGER", 1, 0),
                ("source_file_count", "INTEGER", 1, 0),
                ("report_digest", "TEXT", 1, 0),
                ("report_json", "TEXT", 1, 0),
                ("verified_by", "TEXT", 1, 0),
                ("verified_at", "TEXT", 1, 0),
            )
        for table, expected in expected_columns.items():
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
            actual = tuple(
                (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5])) for row in rows
            )
            if actual != expected:
                raise GovernanceMigrationError(
                    "required schema shape 불일치: "
                    f"table={table} expected={expected} actual={actual}"
                )
        expected_create_sql = {
            statement.split("CREATE TABLE ", 1)[1].split(maxsplit=1)[0]: " ".join(statement.split())
            for migration in self.migrations
            if migration.version <= schema_version
            for statement in migration.statements
            if "CREATE TABLE " in statement
        }
        for table, expected_sql in expected_create_sql.items():
            if schema_version >= 7 and table == "governance_audit_events":
                expected_sql = self._expected_altered_audit_sql()
            if schema_version >= 14 and table == "governance_active_proposals":
                expected_sql = self._expected_altered_active_proposal_sql()
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            actual_sql = " ".join(str(row[0]).split()) if row is not None else ""
            if actual_sql != expected_sql:
                raise GovernanceMigrationError(f"required schema SQL 불일치: table={table}")
        expected_schema_objects = {
            (
                "index" if "CREATE UNIQUE INDEX " in statement else "trigger",
                statement.split(
                    "CREATE UNIQUE INDEX "
                    if "CREATE UNIQUE INDEX " in statement
                    else "CREATE TRIGGER ",
                    1,
                )[1].split(maxsplit=1)[0],
            ): " ".join(statement.split())
            for migration in self.migrations
            if migration.version <= schema_version
            for statement in migration.statements
            if "CREATE UNIQUE INDEX " in statement or "CREATE TRIGGER " in statement
        }
        for (object_type, object_name), expected_sql in expected_schema_objects.items():
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
                (object_type, object_name),
            ).fetchone()
            actual_sql = " ".join(str(row[0]).split()) if row is not None else ""
            if actual_sql != expected_sql:
                raise GovernanceMigrationError(
                    f"required schema SQL 불일치: {object_type}={object_name}"
                )
        metadata = connection.execute(
            "SELECT value FROM governance_store_metadata WHERE key = 'store_kind'"
        ).fetchone()
        if metadata != ("amplai-governance",):
            raise GovernanceMigrationError("Governance Store metadata가 올바르지 않습니다.")
        if schema_version >= 2:
            foreign_keys = connection.execute(
                "PRAGMA foreign_key_list(governance_definition_revisions)"
            ).fetchall()
            if len(foreign_keys) != 3 or any(
                str(row[2]) != "governance_active_proposals" for row in foreign_keys
            ):
                raise GovernanceMigrationError(
                    "definition revision foreign key가 올바르지 않습니다."
                )

    def apply_pending(self, connection: sqlite3.Connection) -> int:
        if not connection.in_transaction:
            raise GovernanceMigrationError(
                "migration entrypoint는 active transaction이 필요합니다."
            )
        self._ensure_history_table(connection)
        current = self._verify_rows(self._applied_rows(connection))
        for migration in self.migrations:
            if migration.version <= current:
                continue
            for statement in migration.statements:
                connection.execute(statement)
            if migration.version == 7:
                self._backfill_audit_manifests(connection)
            connection.execute(
                """
                INSERT INTO governance_schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (migration.version, migration.name, migration.checksum),
            )
        return self.latest_version

    def _expected_altered_audit_sql(self) -> str:
        base = next(
            statement
            for statement in self.migrations[5].statements
            if "CREATE TABLE governance_audit_events" in statement
        )
        alters = tuple(
            statement
            for migration in self.migrations[6:]
            for statement in migration.statements
            if statement.startswith("ALTER TABLE governance_audit_events")
        )
        with sqlite3.connect(":memory:") as fixture:
            fixture.execute(base)
            for statement in alters:
                fixture.execute(statement)
            row = fixture.execute(
                """
                SELECT sql FROM sqlite_master
                WHERE type = 'table' AND name = 'governance_audit_events'
                """
            ).fetchone()
        if row is None:
            raise GovernanceMigrationError("audit schema fixture를 생성할 수 없습니다.")
        return " ".join(str(row[0]).split())

    def _expected_altered_active_proposal_sql(self) -> str:
        base = next(
            statement
            for statement in self.migrations[1].statements
            if "CREATE TABLE governance_active_proposals" in statement
        )
        alters = tuple(
            statement
            for migration in self.migrations[2:]
            for statement in migration.statements
            if statement.strip().startswith("ALTER TABLE governance_active_proposals")
        )
        with sqlite3.connect(":memory:") as fixture:
            fixture.execute(base)
            for statement in alters:
                fixture.execute(statement)
            row = fixture.execute(
                """
                SELECT sql FROM sqlite_master
                WHERE type = 'table' AND name = 'governance_active_proposals'
                """
            ).fetchone()
        if row is None:
            raise GovernanceMigrationError("active proposal schema fixture를 생성할 수 없습니다.")
        return " ".join(str(row[0]).split())

    @staticmethod
    def _backfill_audit_manifests(connection: sqlite3.Connection) -> None:
        """Upgrade v6 audit rows to the manifest-bound v7 hash contract."""

        aggregates = connection.execute(
            """
            SELECT project_namespace, project_id, proposal_id
            FROM governance_aggregate_sequences
            ORDER BY project_namespace, project_id, proposal_id
            """
        ).fetchall()
        for namespace, project_id, proposal_id in aggregates:
            previous_hash: str | None = None
            rows = connection.execute(
                """
                SELECT event_id, command_id, event_type, aggregate_sequence,
                       actor_id, actor_type, policy_snapshot_id, before_state,
                       after_state, definition_digest, occurred_at
                FROM governance_audit_events
                WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                ORDER BY aggregate_sequence
                """,
                (namespace, project_id, proposal_id),
            ).fetchall()
            for row in rows:
                decision = connection.execute(
                    """
                    SELECT idempotency_key FROM governance_decision_results
                    WHERE idempotency_key = ?
                      AND project_namespace = ? AND project_id = ? AND proposal_id = ?
                    """,
                    (row[1], namespace, project_id, proposal_id),
                ).fetchone()
                if decision is None:
                    raise GovernanceMigrationError(
                        "v6 Audit에 대응하는 Decision result가 없습니다."
                    )
                command_id = (
                    "decision:sha256:"
                    + hashlib.sha256(str(decision[0]).encode("utf-8")).hexdigest()
                )
                destinations = connection.execute(
                    """
                    SELECT destination_ref, supersession_key
                    FROM governance_outbox_events
                    WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                      AND aggregate_sequence = ?
                    ORDER BY destination_ref
                    """,
                    (namespace, project_id, proposal_id, row[3]),
                ).fetchall()
                if not destinations:
                    raise GovernanceMigrationError("v6 Audit Outbox manifest가 비어 있습니다.")
                manifest = {
                    "destinations": [
                        {
                            "destination_ref": str(destination[0]),
                            "supersession_key": (
                                str(destination[1]) if destination[1] is not None else None
                            ),
                        }
                        for destination in destinations
                    ]
                }
                manifest_digest = MigrationRunner._digest_json(manifest)
                event_payload = {
                    "actor_id": str(row[4]),
                    "actor_type": str(row[5]),
                    "after_state": str(row[8]),
                    "aggregate_sequence": int(row[3]),
                    "before_state": str(row[7]),
                    "command_id": command_id,
                    "definition_digest": str(row[9]),
                    "destination_count": len(destinations),
                    "destination_manifest_digest": manifest_digest,
                    "event_id": str(row[0]),
                    "event_type": str(row[2]),
                    "occurred_at": str(row[10]),
                    "policy_snapshot_id": str(row[6]),
                    "previous_event_hash": previous_hash,
                    "proposal_ref": {
                        "project_ref": {
                            "namespace": str(namespace),
                            "project_id": str(project_id),
                        },
                        "proposal_id": str(proposal_id),
                    },
                }
                event_hash = MigrationRunner._digest_json(event_payload)
                connection.execute(
                    """
                    UPDATE governance_audit_events
                    SET command_id = ?, destination_manifest_digest = ?,
                        destination_count = ?, previous_event_hash = ?, event_hash = ?
                    WHERE event_id = ?
                    """,
                    (
                        command_id,
                        manifest_digest,
                        len(destinations),
                        previous_hash,
                        event_hash,
                        row[0],
                    ),
                )
                previous_hash = event_hash
            connection.execute(
                """
                UPDATE governance_aggregate_sequences SET last_event_hash = ?
                WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                """,
                (previous_hash, namespace, project_id, proposal_id),
            )

    @staticmethod
    def _digest_json(payload: object) -> str:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"
