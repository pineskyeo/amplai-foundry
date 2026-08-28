from pathlib import Path

import pytest

from amplai_foundry.governance.migrations import GovernanceMigrationError, MigrationRunner
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


# ---------------------------------------------------------------------------
# MGC-012-P5-T014 — migration history 연속성과 store 신원 (round 11 R-9)
# ---------------------------------------------------------------------------


def _initialized_store(tmp_path: Path) -> GovernanceStore:
    store = GovernanceStore(tmp_path / "governance.db")
    store.initialize()
    return store


# T014 AC-01 — 적용 기록에 빈틈이 있으면 store 를 열지 않는다.
#
# 이 검사가 없으면 migration 하나가 적용되지 않은 store 가 정상으로 통과한다. 그 store 에는
# 없는 table 이나 없는 제약이 있고, 그 위의 모든 governed 판단이 근거를 잃는다.
#
# **중간 version 을 지운다.** 끝을 지우면 그냥 낮은 version 이라 연속성이 유지되고, 큰 값을
# 넣으면 위의 미지원 future version 분기가 먼저 잡는다. 겨냥한 분기를 통과시키려면 빈틈이
# 가운데 있어야 한다.
def test_a_gap_in_the_migration_history_is_rejected(tmp_path: Path) -> None:
    store = _initialized_store(tmp_path)
    runner = MigrationRunner()

    with store.connect() as connection:
        applied = runner.verify(connection)
        assert applied == 32, "전제: 온전한 store 는 통과한다"
        connection.execute("DELETE FROM governance_schema_migrations WHERE version = 5")
        connection.commit()

        with pytest.raises(GovernanceMigrationError) as caught:
            runner.verify(connection)

    # T014 AC-04 — 미지원 future version 분기가 아니라 연속성 분기가 잡았음을 보인다.
    message = str(caught.value)
    assert "연속적이지 않습니다" in message
    assert "지원하지 않는" not in message
    assert "현재 contract와 다릅니다" not in message


# T014 AC-01 보조 — 빈틈 하나만으로 실패한다. 여러 곳을 동시에 망가뜨리면 무엇이 잡았는지
# 알 수 없어 고정을 잘못 주장하게 된다.
def test_an_intact_migration_history_still_passes(tmp_path: Path) -> None:
    store = _initialized_store(tmp_path)
    runner = MigrationRunner()

    with store.connect() as connection:
        assert runner.verify(connection) == 32


# T014 AC-02 — store 신원 metadata 가 어긋나면 schema 검증이 거부한다.
#
# 이 값이 이 파일을 governed store 로 지목한다. 검사를 잃으면 아무 SQLite 파일이나
# governance store 로 열리고, 그 위에 governed mutation 이 쌓인다.
@pytest.mark.parametrize(
    "tamper",
    [
        "UPDATE governance_store_metadata SET value = 'something-else' WHERE key = 'store_kind'",
        "DELETE FROM governance_store_metadata WHERE key = 'store_kind'",
    ],
)
def test_a_wrong_store_identity_is_rejected(tmp_path: Path, tamper: str) -> None:
    store = _initialized_store(tmp_path)
    runner = MigrationRunner()

    with store.connect() as connection:
        runner.verify_schema(connection, 32)
        connection.execute(tamper)
        connection.commit()

        with pytest.raises(GovernanceMigrationError) as caught:
            runner.verify_schema(connection, 32)

    assert "metadata가 올바르지 않습니다" in str(caught.value)
