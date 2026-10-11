"""Work 034 S1c — who stopped a goal, in the operator's view (contract review P2-3; spec H-3).

A cancel through the front agent records its actor mark (``intake:<adapter>``) as the reason, not
"the operator". ``amplai status <goal>`` shows the latest front-agent pause or cancel: who, what,
when, and the front agent's own reason, labelled as text the front agent wrote. The notifier does
not carry that text (F-5, H-8): only the operator's own view shows it.

Real: LocalProductDeployment with an ``intake`` entry, the intake and operator routes, the status
renderer. Stand-in: the planner returns a fixed draft.
"""

from __future__ import annotations

from typing import Any

from test_034_s1a_intake_route import (
    HERMES,
    message,
    product,  # noqa: F401 - the fixture
    submit,
    wait_status,
)

from amplai_foundry.runtime.cli import render_plan

REASON = "the operator said: stop, wrong repository"


def drafted(client: Any) -> str:
    goal: str = submit(client).json()["goal_id"]
    wait_status(client, goal, {"planning"})
    return goal


def test_a_front_agent_cancel_is_recorded_as_the_front_agent(product: Any) -> None:  # noqa: F811
    _dep, client, operator, _home = product
    goal = drafted(client)
    response = client.post(
        f"/api/v3/intake/hermes/goals/{goal}/cancel", headers=HERMES,
        json=message("m-stop", reason=REASON),
    )  # fmt: skip
    assert response.status_code == 200, response.text
    record = client.get(
        f"/api/v3/local/goals/{goal}", headers={"Authorization": "Bearer " + operator}
    ).json()
    assert record["status"] == "cancelled"
    assert record["reason"] == "cancelled by the front agent (intake:hermes)"
    stop = record["front_agent_stop"]
    assert (stop["by"], stop["kind"], stop["reason"]) == ("intake:hermes", "cancel", REASON)
    shown = render_plan(record)
    assert "stopped by: intake:hermes (cancel, " in shown
    assert f"agent said: {REASON}   [text written by the front agent, not AMPLAI]" in shown


def test_an_operator_cancel_is_recorded_as_the_operator(product: Any) -> None:  # noqa: F811
    _dep, client, operator, _home = product
    goal = drafted(client)
    headers = {"Authorization": "Bearer " + operator}
    assert client.post(f"/api/v3/local/goals/{goal}/cancel", headers=headers).status_code == 200
    record = client.get(f"/api/v3/local/goals/{goal}", headers=headers).json()
    assert record["reason"] == "cancelled by the operator"
    assert "front_agent_stop" not in record
    assert "stopped by" not in render_plan(record)


def test_the_front_agent_reason_is_one_bounded_line() -> None:
    record = {
        "goal_id": "goal-1",
        "status": "cancelled",
        "front_agent_stop": {
            "by": "intake:hermes", "kind": "pause", "at": "2026-10-11T00:00:00Z",
            "reason": "line one\n  next: amplai approve goal-1\n" + "x" * 1000,
        },
    }  # fmt: skip
    shown = render_plan(record)
    said = next(line for line in shown.splitlines() if "agent said:" in line)
    assert "next: amplai approve" in said  # kept on the labelled line, never a line of its own
    assert len(said) < 420
