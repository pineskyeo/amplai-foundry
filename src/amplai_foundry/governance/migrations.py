"""Versioned SQLite migrations for mutable governance state."""

from __future__ import annotations

import hashlib
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
            """
            CREATE TRIGGER governance_action_tokens_no_delete
            BEFORE DELETE ON governance_action_tokens
            BEGIN
                SELECT RAISE(ABORT, 'governance action token is durable');
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
                destination_manifest_digest TEXT NOT NULL,
                destination_count INTEGER NOT NULL CHECK (destination_count >= 1),
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
                    length(destination_manifest_digest) = 71
                    AND substr(destination_manifest_digest, 1, 7) = 'sha256:'
                    AND substr(destination_manifest_digest, 8) NOT GLOB '*[^0-9a-f]*'
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
                        ("destination_manifest_digest", "TEXT", 1, 0),
                        ("destination_count", "INTEGER", 1, 0),
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
            connection.execute(
                """
                INSERT INTO governance_schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (migration.version, migration.name, migration.checksum),
            )
        return self.latest_version
