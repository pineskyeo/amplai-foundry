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
                ),
                CHECK (
                    length(candidate_commit) IN (40, 64)
                    AND candidate_commit NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    length(canonical_ref) BETWEEN 12 AND 255
                    AND substr(canonical_ref, 1, 11) = 'refs/heads/'
                    AND canonical_ref NOT GLOB '*[[:space:]~^:?*\\[]*'
                    AND substr(canonical_ref, -1) != '/'
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
