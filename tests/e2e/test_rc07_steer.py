"""Work 019 S4 (D) — operator steering of a running agent (D-082).

The design's pause → exact-session resume pair on the product path: the running process is
stopped at a boundary and checkpointed, the same native session resumes with the operator's
message, and the result is verified as usual. Stand-ins as in the rc06 rig; the real steer is
recorded under specs/019-v3-completion/runs/.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.evaluation.observatory import Observatory
from amplai_foundry.runtime.errors import Hold
from rc06_rig import approved, rig_with_codex, with_permissions


def wait_running(rig: Any, deadline: float = 20.0) -> None:
    end = time.time() + deadline
    while time.time() < end:
        rows = rig.d.store.conn.execute(
            "SELECT state FROM heads WHERE kind='run' AND tenant=? AND project=?",
            rig.d.scope.keys(),
        ).fetchall()
        if any(r["state"] == "running" for r in rows):
            return
        time.sleep(0.05)
    raise AssertionError("the attempt never started")


def test_steering_pauses_and_resumes_the_same_session_with_the_message(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, container = rig_with_codex(deployment, tmp_path, "steer")
    operator = with_permissions(rig.operator, "goal.steer")
    goal = approved(rig)
    result: dict[str, Any] = {}
    runner = threading.Thread(target=lambda: result.update(loop.run_goal(goal)))
    runner.start()
    wait_running(rig)
    queued = loop.steer(operator, goal, "value() must return 2")
    assert queued["status"] == "steering"
    runner.join(60)
    assert result["status"] == "published"
    (attempt,) = result["attempts"]
    assert attempt["outcome"] == "pass" and attempt["steered"] is True
    # the resumed turn got the operator's message on the same native session
    assert "Operator steering" in container.prompts[1]
    assert "value() must return 2" in container.prompts[1]
    record = rig.service.plan_record(goal)
    assert [s["text"] for s in record["steering"]] == ["value() must return 2"]
    d = rig.d
    boundary = d.store.conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='steering.process_boundary'"
    ).fetchone()[0]
    assert boundary == 1
    # the contract never changed; steering counts as human intervention
    assert (
        d.store.head(d.scope, "goal", goal)["data"]["active_contract_ref"] == record["contract_ref"]
    )
    events = Observatory(d.store).summary(d.scope)["human_intervention_events"]
    assert any(k.startswith("steering.") for k in events)
    # no credential stays in any session home: released at the pause and after the resumed turn,
    # and the resumed turn's token refresh came back to the scoped copy
    assert list((tmp_path / "journal").rglob("auth.json")) == []
    assert (rig.home / ".codex" / "auth.json").read_text() == '{"tokens": "refreshed"}'


def test_steering_outside_a_running_attempt_is_refused(deployment: Any, tmp_path: Path) -> None:
    rig, loop, _container = rig_with_codex(deployment, tmp_path, "right")
    operator = with_permissions(rig.operator, "goal.steer")
    goal = approved(rig)  # approved, not running
    with pytest.raises(Hold) as held:
        loop.steer(operator, goal, "anything")
    assert held.value.code == "STEER_NOT_RUNNING"
