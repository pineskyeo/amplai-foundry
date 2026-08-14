from pathlib import Path

from amplai_foundry.governance.migrations import MigrationRunner
from amplai_foundry.governance.store import GovernanceStore


def test_review_card_schema_is_version_32_and_exact(tmp_path: Path) -> None:
    store = GovernanceStore(tmp_path / "governance.db")
    store.initialize()

    with store.connect() as connection:
        runner = MigrationRunner()
        assert runner.current_version(connection) == 32
        runner.verify_schema(connection, 32)
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'index'"
            ).fetchall()
        }

    assert "governance_review_card_commands" in tables
    assert "governance_review_action_sets" in tables
    assert "governance_review_card_one_snapshot" in indexes
    assert "governance_review_card_one_channel_scope" in indexes
