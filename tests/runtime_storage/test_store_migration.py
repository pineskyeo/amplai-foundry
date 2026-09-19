"""V3-007 storage tier — commands migration and owner lifecycle (design/09 §4).

`tests/v3/test_rc01_storage_uow.py` carries the catalog cases; this package holds the
storage-only invariants that no catalog id names.
"""

from __future__ import annotations

import sqlite3

from amplai_foundry.runtime.storage.store import Scope, Store


def _scope() -> Scope:
    return Scope(tenant_id="t", project_id="p")


def test_commands_table_without_operation_is_migrated_in_place(tmp_path):
    root = tmp_path / "state"
    with Store(root) as first:
        first.command(_scope(), "actor", "legacy-key", {"x": 1}, lambda db: {"ok": 1})
        first.conn.executescript(
            """
            CREATE TABLE commands_old AS SELECT tenant,project,actor,key,request_digest,
              result,created_at FROM commands;
            DROP TABLE commands;
            ALTER TABLE commands_old RENAME TO commands;
            """
        )
        columns = {row[1] for row in first.conn.execute("PRAGMA table_info(commands)")}
        assert "operation" not in columns

    with Store(root) as second:
        columns = {row[1] for row in second.conn.execute("PRAGMA table_info(commands)")}
        assert {"operation", "retention_class"} <= columns
        row = second.conn.execute(
            "SELECT operation, retention_class FROM commands WHERE key='legacy-key'"
        ).fetchone()
        assert tuple(row) == ("command", "command")
        # The migrated row still short-circuits an exact repeat.
        effects: list[int] = []

        def op(db: sqlite3.Connection) -> dict[str, int]:
            effects.append(1)
            return {"ok": 2}

        assert second.command(_scope(), "actor", "legacy-key", {"x": 1}, op) == {"ok": 1}
        assert effects == []


def test_close_releases_owner_lock_and_connection(tmp_path):
    root = tmp_path / "state"
    store = Store(root)
    store.close()
    assert not hasattr(store, "conn")
    # A second close is a no-op and a new owner can open the same root.
    store.close()
    with Store(root) as reopened:
        assert "filesystem" in reopened.storage_qualification
