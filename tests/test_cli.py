"""CLI surface tests.

현재는 `amplai-foundry governance` 조회 두 개만 다룬다 (MGC-012-P5-T026, `D-044`).
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

# governance state 를 만드는 fixture 는 `test_slack_ack_boundary` 에 이미 있다. 복제하면
# 두 벌이 어긋난다. top-level import 는 pyproject 의 `pythonpath = ["tests"]` 가 받친다.
import test_slack_ack_boundary as ack_fixtures
from amplai_foundry.cli import app
from amplai_foundry.governance import IngressConfig, IngressService, IngressState
from amplai_foundry.governance.filesystem import FilesystemStatus, PlatformFilesystemProbe
from amplai_foundry.governance.store import GovernanceStore, GovernanceStoreError

RUNNER = CliRunner()


def _workspace(tmp_path: Path) -> Path:
    """`cli.py` 가 기대하는 `.amplai/runtime/governance.db` 자리를 만든다."""
    workspace = tmp_path / "workspace"
    (workspace / ".amplai" / "runtime").mkdir(parents=True)
    return workspace


def _store(workspace: Path) -> GovernanceStore:
    store = GovernanceStore(workspace / ".amplai/runtime/governance.db")
    store.initialize()
    return store


def _stranded_command(workspace: Path) -> tuple[GovernanceStore, str]:
    """소진되어 recovery hold 로 남은 command 하나를 만든다."""
    store = _store(workspace)
    ack_fixtures._seed(store)
    now = [ack_fixtures.NOW]
    ingress = IngressService(
        store,
        ack_fixtures._authenticator(),
        config=IngressConfig(max_attempts=2),
        clock=lambda: now[0],
    )
    ack = ack_fixtures.BoundedIngressAck(
        ingress, monotonic=ack_fixtures._monotonic(0.0, 0.05)
    ).submit(ack_fixtures._envelope())
    worker = ack_fixtures._worker(store, ingress)

    def _raise(*args: object, **kwargs: object) -> object:
        raise GovernanceStoreError("ingress decision connection is busy")

    worker.decisions.decide_ingress_in_transaction = _raise  # type: ignore[method-assign]
    for _ in range(2):
        worker.process_next("cli-test-worker")
        now[0] = now[0] + timedelta(minutes=10)

    settled = ingress.get(str(ack.command_id))
    assert settled is not None
    assert settled.state is IngressState.RECOVERY_HOLD
    return store, str(ack.command_id)


def _decided_command(workspace: Path) -> str:
    """결정이 실제로 commit 된 command 하나를 만든다."""
    store = _store(workspace)
    ack_fixtures._seed(store)
    ingress = IngressService(store, ack_fixtures._authenticator(), clock=lambda: ack_fixtures.NOW)
    ack = ack_fixtures.BoundedIngressAck(
        ingress, monotonic=ack_fixtures._monotonic(0.0, 0.05)
    ).submit(ack_fixtures._envelope())
    result = ack_fixtures._worker(store, ingress).process_next("cli-test-worker")
    assert result is not None
    assert result.decision is not None
    return str(ack.command_id)


# T026 AC-01 — 착수 전 상태의 기록. 이 둘을 부를 수 있는 사람 진입점이 없었다.
def test_the_governance_subapp_exposes_exactly_the_two_reads() -> None:
    result = RUNNER.invoke(app, ["governance", "--help"])

    assert result.exit_code == 0
    assert "stranded" in result.stdout
    assert "decision" in result.stdout
    # governed mutation 을 여는 command 가 없다 (`D-044` 의 범위 제한).
    for forbidden in ("release", "resume", "retry", "apply", "approve", "delete"):
        assert forbidden not in result.stdout


# T026 AC-02
def test_stranded_lists_a_command_no_worker_will_claim_again(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _store_unused, command_id = _stranded_command(workspace)

    result = RUNNER.invoke(app, ["governance", "stranded", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.stdout
    assert command_id in result.stdout
    assert "recovery_hold" in result.stdout
    assert "attempts=2" in result.stdout


def test_stranded_reports_state_attempts_and_error_code(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _store_unused, command_id = _stranded_command(workspace)

    result = RUNNER.invoke(app, ["governance", "stranded", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.stdout
    line = result.stdout.strip()
    assert line.startswith(command_id)
    assert "recovery_hold" in line
    assert "attempts=2" in line
    assert line.split()[-1] != "-", "last_error_code 가 비어 있다"


def test_stranded_says_none_when_nothing_is_stranded(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _store(workspace)

    result = RUNNER.invoke(app, ["governance", "stranded", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.stdout
    assert result.stdout.strip() == "NONE"


# T026 AC-03
def test_decision_answers_that_a_decision_was_committed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    command_id = _decided_command(workspace)

    result = RUNNER.invoke(
        app, ["governance", "decision", command_id, "--workspace", str(workspace)]
    )

    assert result.exit_code == 0, result.stdout
    assert "approve" in result.stdout
    assert "approved" in result.stdout


# T026 AC-04
def test_decision_answers_none_for_a_command_that_never_decided(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _store_unused, command_id = _stranded_command(workspace)

    result = RUNNER.invoke(
        app, ["governance", "decision", command_id, "--workspace", str(workspace)]
    )

    assert result.exit_code == 0, result.stdout
    assert result.stdout.strip() == "NONE"


# T026 AC-05 — 조회가 credential 을 내지 않는다.
def test_neither_read_exposes_a_credential(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    command_id = _decided_command(workspace)

    outputs = [
        RUNNER.invoke(app, ["governance", "stranded", "--workspace", str(workspace)]).stdout,
        RUNNER.invoke(
            app, ["governance", "decision", command_id, "--workspace", str(workspace)]
        ).stdout,
    ]
    combined = "\n".join(outputs)

    assert ack_fixtures.RAW_TOKEN not in combined
    assert "TOK-" not in combined, "token id 를 내보내지 않는다"
    assert ack_fixtures.SIGNING_SECRET not in combined


# T026 AC-06 — 조회가 durable state 를 바꾸지 않는다.
def test_the_reads_do_not_claim_lease_or_mutate_anything(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    store, command_id = _stranded_command(workspace)

    def snapshot() -> list[tuple[object, ...]]:
        with store.connect() as connection:
            return [
                tuple(row)
                for row in connection.execute(
                    "SELECT command_id, state, attempts, claim_generation, lease_owner, "
                    "lease_expires_at, last_error_code FROM governance_ingress_commands "
                    "ORDER BY command_id"
                ).fetchall()
            ]

    before = snapshot()
    RUNNER.invoke(app, ["governance", "stranded", "--workspace", str(workspace)])
    RUNNER.invoke(app, ["governance", "decision", command_id, "--workspace", str(workspace)])

    assert snapshot() == before, "조회가 durable state 를 바꿨다"


# T026 AC-07 — 실패는 기존 CLI 오류 처리와 같은 형태다.
def test_a_missing_store_fails_like_every_other_command(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    result = RUNNER.invoke(app, ["governance", "stranded", "--workspace", str(workspace)])

    assert result.exit_code == 1
    assert "Traceback" not in result.stdout
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_an_invalid_limit_fails_closed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _stranded_command(workspace)

    result = RUNNER.invoke(
        app, ["governance", "stranded", "--workspace", str(workspace), "--limit", "0"]
    )

    assert result.exit_code == 1
    assert "Traceback" not in result.stdout


# ---------------------------------------------------------------------------
# MGC-012-P5-T028 — 조회가 실제로 read-only 다 (round 15 C-1, F-3, F-4, C-3)
# ---------------------------------------------------------------------------


def test_a_store_on_a_network_filesystem_fails_without_a_traceback(tmp_path: Path) -> None:
    """round 15 `C-1`. 예전에는 raw `GovernanceFilesystemError` 가 나갔다.

    `check_startup()` 이 `connect()` **밖에서** filesystem 을 검증해서 `T024` 의 정규화가
    닿지 않았다. 그 호출을 뺐으므로 이제 `connect()` 안에서 정규화된다.
    """
    workspace = _workspace(tmp_path)
    _store(workspace)

    def _network(self: object, path: Path) -> FilesystemStatus:
        return FilesystemStatus(kind="nfs", mount_point=path.parent, local=False)

    with patch.object(PlatformFilesystemProbe, "inspect", _network):
        for argv in (
            ["governance", "stranded", "--workspace", str(workspace)],
            ["governance", "decision", "CMD-0000000000000000", "--workspace", str(workspace)],
        ):
            result = RUNNER.invoke(app, argv)
            assert result.exit_code == 1, argv
            assert result.exception is None or isinstance(result.exception, SystemExit), (
                f"raw traceback 이 나갔다: {result.exception!r}"
            )


def test_the_reads_do_not_contend_with_a_worker_holding_a_write_lock(tmp_path: Path) -> None:
    """round 15 `F-3`. 예전에는 `WAL/SHM write probe 실패` 로 죽었다.

    회수 도구가 회수해야 할 바로 그 순간에 안 됐다.
    """
    workspace = _workspace(tmp_path)
    _store_unused, command_id = _stranded_command(workspace)

    holder = GovernanceStore(workspace / ".amplai/runtime/governance.db")
    with holder.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE governance_store_metadata SET value = value WHERE key = 'store_kind'"
        )
        try:
            listed = RUNNER.invoke(app, ["governance", "stranded", "--workspace", str(workspace)])
            decided = RUNNER.invoke(
                app, ["governance", "decision", command_id, "--workspace", str(workspace)]
            )
        finally:
            connection.execute("ROLLBACK")

    assert listed.exit_code == 0, listed.stdout + listed.stderr
    assert command_id in listed.stdout
    assert decided.exit_code == 0, decided.stdout + decided.stderr


def test_the_reads_issue_no_write_statement(tmp_path: Path) -> None:
    """round 15 `F-4`. "조회만" 이라고 적으려면 write 가 없어야 한다.

    `sqlite3.Connection.execute` 는 immutable type 이라 patch 할 수 없다.
    `set_trace_callback` 으로 실제로 실행된 SQL 을 받는다 — 더 강한 관측이다.
    """
    workspace = _workspace(tmp_path)
    _store_unused, command_id = _stranded_command(workspace)
    seen: list[str] = []

    original_connect = sqlite3.connect

    def _tracing_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = original_connect(*args, **kwargs)  # type: ignore[arg-type]
        connection.set_trace_callback(seen.append)
        return connection

    with patch.object(sqlite3, "connect", _tracing_connect):
        listed = RUNNER.invoke(app, ["governance", "stranded", "--workspace", str(workspace)])
        decided = RUNNER.invoke(
            app, ["governance", "decision", command_id, "--workspace", str(workspace)]
        )

    assert listed.exit_code == 0, listed.stdout + listed.stderr
    assert decided.exit_code == 0, decided.stdout + decided.stderr
    assert seen, "SQL 을 하나도 관측하지 못했다 — 측정이 실패했다"
    mutating = [
        sql
        for sql in seen
        if any(
            token in " ".join(sql.upper().split())
            for token in ("BEGIN IMMEDIATE", "UPDATE ", "INSERT ", "DELETE ")
        )
    ]
    assert mutating == [], f"조회가 write 를 냈다: {mutating}"


def test_a_missing_store_is_not_created_by_a_read(tmp_path: Path) -> None:
    """없는 store 를 열어 `NONE` 을 내면 operator 를 속인다."""
    workspace = _workspace(tmp_path)
    path = workspace / ".amplai/runtime/governance.db"

    result = RUNNER.invoke(app, ["governance", "stranded", "--workspace", str(workspace)])

    assert result.exit_code == 1
    assert not path.exists(), "조회가 빈 store 를 만들었다"


def test_the_reads_reject_a_json_flag(tmp_path: Path) -> None:
    """round 15 `C-3`. `T026` 의 `scope.exclude` 가 배제한 것을 되돌렸다."""
    workspace = _workspace(tmp_path)
    _store(workspace)

    result = RUNNER.invoke(app, ["governance", "stranded", "--workspace", str(workspace), "--json"])

    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# MGC-012-P5-T032 — CLI 경계에서 정규화한다 (round 16 FR-3, R16-1, C16-1)
# ---------------------------------------------------------------------------
#
# round 16 이 CLI raw traceback 을 **두 갈래**로 다시 냈다. `FR-3` 은 T029 의
# `_OPEN_FAILURES` 축소가, `R16-1` 은 T028 의 `check_startup()` 제거가 원인이다.
# 전수로 재니 네 상태가 raw traceback 이었다 — 위 둘에 빈 파일과 손상 파일이 더 있었다.
#
# **정규화 위치가 요점이다.** `connect()` 에 넣으면 `legacy_*.py` 의 `try` 열이
# `sqlite3.Error` 봉쇄를 잃는다 (round 15 `R-1`). CLI 가 최종 소비자다.


def _readonly(path: Path) -> None:
    for child in path.parent.iterdir():
        child.chmod(0o444)
    path.parent.chmod(0o555)


def _writable(path: Path) -> None:
    path.parent.chmod(0o755)
    for child in path.parent.iterdir():
        child.chmod(0o644)


def _foreign_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE not_governance(a)")
    connection.commit()
    connection.close()


def _corrupt_database(path: Path) -> None:
    path.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4000)


def _each_read(workspace: Path) -> list[tuple[str, object]]:
    results = []
    for argv in (
        ["governance", "stranded", "--workspace", str(workspace)],
        ["governance", "decision", "CMD-0000000000000000", "--workspace", str(workspace)],
    ):
        result = RUNNER.invoke(app, argv)
        results.append((argv[1], result))
    return results


@pytest.mark.parametrize(
    "damage",
    ["read-only", "foreign-schema", "empty-file", "corrupt-file"],
)
def test_no_store_state_makes_a_read_print_a_traceback(tmp_path: Path, damage: str) -> None:
    """store 상태 전수. 하나라도 traceback 이면 실패다."""
    workspace = _workspace(tmp_path)
    path = workspace / ".amplai/runtime/governance.db"
    if damage == "read-only":
        _store(workspace)
        _readonly(path)
    elif damage == "foreign-schema":
        _foreign_database(path)
    elif damage == "empty-file":
        path.touch()
    else:
        _store(workspace)
        _corrupt_database(path)

    try:
        for name, result in _each_read(workspace):
            assert result.exit_code == 1, f"{damage}/{name}"
            assert result.exception is None or isinstance(result.exception, SystemExit), (
                f"{damage}/{name} 에서 raw traceback 이 나갔다: {result.exception!r}"
            )
    finally:
        if damage == "read-only":
            _writable(path)


def test_decision_tells_unknown_apart_from_no_decision(tmp_path: Path) -> None:
    """round 16 `C16-1`. 읽을 수 없는 row 에 '결정 없음' 을 내면 거짓 음성이다."""
    workspace = _workspace(tmp_path)
    store, command_id = _stranded_command(workspace)

    absent = RUNNER.invoke(
        app, ["governance", "decision", command_id, "--workspace", str(workspace)]
    )
    assert absent.exit_code == 0, absent.stdout
    assert absent.stdout.strip() == "NONE"

    with store.connect() as connection:
        connection.execute(
            "UPDATE governance_ingress_commands SET channel_json = ? WHERE command_id = ?",
            ("{bad", command_id),
        )
        connection.commit()

    unknown = RUNNER.invoke(
        app, ["governance", "decision", command_id, "--workspace", str(workspace)]
    )
    assert unknown.exit_code == 0, unknown.stdout
    assert unknown.stdout.strip() != "NONE", "'알 수 없음' 이 '결정 없음' 으로 보고됐다"
    assert "UNKNOWN" in unknown.stdout


def test_both_reads_reject_a_json_flag(tmp_path: Path) -> None:
    """round 16 `R16-6`. 예전 test 는 `stranded` 만 봤다."""
    workspace = _workspace(tmp_path)
    _store(workspace)

    for argv in (
        ["governance", "stranded", "--workspace", str(workspace), "--json"],
        [
            "governance",
            "decision",
            "CMD-0000000000000000",
            "--workspace",
            str(workspace),
            "--json",
        ],
    ):
        result = RUNNER.invoke(app, argv)
        # typer 는 usage 오류를 exit 2 로 낸다. 문구는 stderr 로 가므로 code 만 본다.
        assert result.exit_code == 2, argv


def test_the_decision_read_survives_a_store_failure_inside_the_unreadable_check(
    tmp_path: Path,
) -> None:
    """round 17 `F17-8`. wave 10 이 놓은 **세 번째** 포획에 test 가 없었다.

    기존 test 넷은 store 를 통째로 망가뜨려서 `committed_decision()` 단계에서 이미 터진다.
    그래서 안쪽 포획에 **도달하지 못한다** — 지워도 suite 가 전부 통과했다. 여기서는
    `committed_decision()` 을 성공시키고 `is_unreadable()` 만 실패시켜 그 지점을 때린다.
    """
    workspace = _workspace(tmp_path)
    _store_unused, command_id = _stranded_command(workspace)

    def _fails(self: IngressService, command_id: str) -> bool:
        raise sqlite3.OperationalError("database disk image is malformed")

    with patch.object(IngressService, "is_unreadable", _fails):
        result = RUNNER.invoke(
            app, ["governance", "decision", command_id, "--workspace", str(workspace)]
        )

    assert result.exit_code == 1, result.stdout + result.stderr
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"raw traceback 이 나갔다: {result.exception!r}"
    )


def _wall_for_cli(workspace: Path, state: str, count: int) -> GovernanceStore:
    """CLI test 용 벽. `ack_fixtures._readable_wall` 을 그대로 쓴다."""
    store = _store(workspace)
    ack_fixtures._readable_wall(store, state, count)
    return store


def test_stranded_says_when_the_list_is_cut(tmp_path: Path) -> None:
    """round 20 `F20-1`(P1)·`F20-2`. `D-050` 이 정한 것.

    **`limit` 이 있는 한 어떤 규칙도 완전할 수 없다.** `SQL LIMIT` 은 읽힐 벽이, python
    출력 상한은 **안 읽힐 벽**이 `limit + 1` 번째를 민다. 없애는 대신 **보이게** 만든다.
    이 줄이 없으면 operator 가 목록이 잘린 것을 알 방법이 없다.
    """
    workspace = _workspace(tmp_path)
    store, _command_id = _stranded_command(workspace)
    ack_fixtures._readable_wall(store, "dead_letter", 5)

    cut = RUNNER.invoke(
        app, ["governance", "stranded", "--workspace", str(workspace), "--limit", "3"]
    )

    assert cut.exit_code == 0, cut.stdout
    assert "잘렸다" in cut.stdout, "목록이 잘렸는데 알리지 않는다"
    rows = [line for line in cut.stdout.splitlines() if line and "잘렸다" not in line]
    assert len(rows) == 3


def test_stranded_stays_quiet_when_the_list_fits(tmp_path: Path) -> None:
    """round 20 `F20-1`. 안 잘렸으면 출력이 이전과 같아야 한다."""
    workspace = _workspace(tmp_path)
    _store_unused, command_id = _stranded_command(workspace)

    whole = RUNNER.invoke(app, ["governance", "stranded", "--workspace", str(workspace)])

    assert whole.exit_code == 0, whole.stdout
    assert "잘렸다" not in whole.stdout, "안 잘렸는데 잘렸다고 말한다"
    assert command_id in whole.stdout


def test_stranded_reports_a_cut_in_either_list(tmp_path: Path) -> None:
    """round 20 `F20-2`. **두 목록을 각각 판정한다.**

    `stranded` 후보는 적고 손상 row 만 많은 경우다. 둘은 다른 조회이고 후보 집합도 다르다.
    """
    workspace = _workspace(tmp_path)
    store, _command_id = _stranded_command(workspace)
    ack_fixtures._readable_wall(store, "pending", 6)
    with store.connect() as connection:
        connection.execute(
            "UPDATE governance_ingress_commands SET channel_json='{bad' WHERE state='pending'"
        )
        connection.commit()

    cut = RUNNER.invoke(
        app, ["governance", "stranded", "--workspace", str(workspace), "--limit", "2"]
    )

    assert cut.exit_code == 0, cut.stdout
    assert "UNREADABLE" in cut.stdout
    assert "잘렸다" in cut.stdout, "손상 목록만 잘려도 알려야 한다"


@pytest.mark.parametrize("bad_limit", ["0", "-1"])
def test_stranded_still_rejects_a_non_positive_limit(tmp_path: Path, bad_limit: str) -> None:
    """round 20 `F20-1` 의 부작용을 막는다.

    CLI 가 `limit + 1` 을 넘기므로 `--limit 0` 은 service 의 `limit < 1` guard 에 **`1` 로
    도착해 우회된다.** 그 guard 가 지금까지 `--limit 0` 을 잡고 있었다 (round 18 `A18-5`,
    round 20 `A20-F3`). **새 구조가 요구한 첫 항목이고 착수 전에 보였다.**
    """
    workspace = _workspace(tmp_path)
    _store_unused, _command_id = _stranded_command(workspace)

    result = RUNNER.invoke(
        app, ["governance", "stranded", "--workspace", str(workspace), "--limit", bad_limit]
    )

    assert result.exit_code == 1, result.stdout + result.stderr
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"raw traceback 이 나갔다: {result.exception!r}"
    )
