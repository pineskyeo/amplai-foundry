"""Work 033 S10: L7 fast checks run as M3 follow-ups before the protected final verification
(interfaces.md §5.1 M3, §5.2 "L7 fast checks", §6.1 L7, §2.2 `fast_checks`, the Clarification after
S9 and S11: "L7 fast checks are built in S10").

Contract read: after a turn the hook runs the app's `quick_verifiers` named by the `fast_checks`
component, in the verify sandbox image with network none, on a copy of the run workspace's change; a
failure becomes one follow-up message with the failing command and a tail of its output (feedback
form rules) resumed into the same session as dispatch `<dispatch_id>-f<k>` (k <= `max_followups` <=
2); fast checks never produce verdicts and the suite after `output_ready` stays the only
verification. The L7 decision (`none` | `quick_checks`, prior `none` in v1) is recorded before the
final verification. M3 rules of §5.1 for the follow-up itself: dispatch id, same session, the
`followups` list of the worker-execution head.

Real: the rc06 rig with the S9 stand-ins (host-process agent that speaks the Codex stream, resumed
for follow-ups), the real worker, `FastCheckHooks`, the quick verifier through the rig's
`SuiteVerifier`, the protected verification (`finish_work`) and the `Decider`. Stand-ins (named):
the "container" runs a script on the host; the quick command and the protected suite are small
Python one-liners (a quick check that fails on the text `return 3`, a protected suite command that
fails on the marker `BAD`). As in a real app, the quick verifier is also one of the commands the
protected suite runs (a quick test is a subset of the full one), so a change that passes the
quick check can still fail the suite, and a change the follow-up leaves unrepaired fails it too.
No network or docker.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from test_033_s10_deciders import (
    Env,
    Ref,
    build_world,
    candidate,
    decisions_of,
    env_of,
    fault,
    only,
    rows,
)

from amplai_foundry.runtime.execution import policies
from amplai_foundry.runtime.execution.loop import FAST_HEADER
from e2e.test_033_s9_strategies import (
    DEFAULT_PARAMS,
    GOOD,
    Call,
    World,
    outcomes,
)

THREE = "def value():\n    return 3\n"  # the quick check refuses this (and so does the suite)
BAD_BUT_QUICK_OK = "def value():\n    return 2  # BAD\n"  # the suite refuses this; quick does not
LIMIT = {**policies.V1["limits"], "aux_max_tokens": 5_000_000}


def first_wrong_then_good(call: Call) -> dict[str, str]:
    return {"app.py": GOOD if call.resume else THREE}


def fast(w: World, **over: Any) -> Ref:
    """The `fast_checks` component version that switches the quick check on."""
    return w.comps.version("fast_checks", enabled=True, checks=["quick"], max_followups=1, **over)


class Trace:
    """What happened, in order: the executor turns, the quick checks and the protected
    verification (`verify`). The quick check goes through `verifier_factory` at call time, the
    protected one through the runner registered at install, so each is observed where it lives."""

    def __init__(self, w: World) -> None:
        self.events: list[str] = []
        self.quick_inputs: list[tuple[str, ...]] = []
        w.agents.on_call = lambda call: self.events.append(
            "turn:resume" if call.resume else "turn:first"
        )
        service = w.service
        make = service.verifier_factory

        def factory(app: Any) -> Any:
            runner = make(app)
            ids = tuple(v.id for v in app.verifiers)

            def run(change: bytes) -> Any:
                self.events.append("quick" if ids == ("quick",) else "suite-factory")
                self.quick_inputs.append(ids)
                return runner(change)

            return run

        service.verifier_factory = factory
        real = service.verification.verify

        def verify(*args: Any, **kwargs: Any) -> Any:
            self.events.append("verify")
            return real(*args, **kwargs)

        service.verification.verify = verify  # type: ignore[method-assign]


def worker_head(w: World, dispatch_id: str) -> dict[str, Any]:
    head: dict[str, Any] = w.rig.d.store.head(w.rig.d.scope, "worker-execution", dispatch_id)
    return head


# ======================================================================================
# 1. a failing quick check is a follow-up turn, before the final verification
# ======================================================================================
def test_a_failing_quick_check_becomes_a_follow_up_before_the_protected_verification(
    deployment: Any, tmp_path: Path
) -> None:
    w = build_world(deployment, tmp_path, first_wrong_then_good, quick=True)
    trace = Trace(w)
    record = w.run(candidate(w, fast_checks=fast(w)))
    assert record["status"] == "published" and outcomes(record) == ["pass"]
    # the order of the contract: the turn, the quick check, the follow-up turn (a resume of the same
    # attempt, not a second attempt), then the protected verification. The follow-up's own change
    # is not quick-checked again: the checks run at most `max_followups` times (M3 rule 1).
    assert trace.events == ["turn:first", "quick", "turn:resume", "verify"]
    first, follow = w.agents.calls
    assert follow.resume is True and follow.session is not None
    assert follow.name == first.name + "-f1"  # IC-04: <dispatch_id>-f<k>, the same run
    prompt = follow.prompt
    assert prompt.startswith(FAST_HEADER) and "quick" in prompt  # the failing command's id
    assert "acceptance commands" in prompt  # the suite still decides after this turn
    # the quick check ran on the quick verifier alone, never on the protected suite command
    assert set(trace.quick_inputs) == {("quick",)}
    attempt = record["attempts"][0]
    assert attempt["followups"] == 1 and attempt["fast_checks"][0]["outcome"] == "fail"
    assert [c["turn"] for c in attempt["fast_checks"]] == [0]
    assert attempt["verdicts"] and all("verdicts" not in c for c in attempt["fast_checks"])
    assert attempt["verdicts"][0]["outcome"] == "pass"  # the protected verification's verdict
    # the worker-execution head lists the follow-up (M3 rule 4)
    (entry,) = worker_head(w, first.name)["data"]["followups"]
    assert entry["dispatch_id"] == first.name + "-f1"
    assert entry["receipt_digest"].startswith("sha256:")


def test_the_follow_up_message_carries_the_failing_command_and_a_tail_of_its_output(
    deployment: Any, tmp_path: Path
) -> None:
    noisy = (
        "python3",
        "-c",
        "import pathlib, sys; t = pathlib.Path('app.py').read_text(); bad = 'return 3' in t; "
        "print('QUICK-OUTPUT-MARKER' if bad else ''); sys.exit(1 if bad else 0)",
    )
    w = build_world(deployment, tmp_path, first_wrong_then_good, quick=True, quick_argv=noisy)
    Trace(w)
    w.run(candidate(w, fast_checks=fast(w)))
    message = w.agents.calls[1].prompt
    assert "quick" in message and "exited 1" in message  # the failing command and its exit code
    assert "QUICK-OUTPUT-MARKER" in message  # a tail of its output (the feedback form's tail)
    assert message.rstrip().endswith("then run the acceptance commands again.")


def test_a_quick_check_that_passes_sends_no_follow_up(deployment: Any, tmp_path: Path) -> None:
    w = build_world(deployment, tmp_path, quick=True)
    trace = Trace(w)
    record = w.run(candidate(w, fast_checks=fast(w)))
    assert record["status"] == "published" and len(w.agents.calls) == 1
    assert trace.events == ["turn:first", "quick", "verify"]
    attempt = record["attempts"][0]
    assert attempt["followups"] == 0 and [c["outcome"] for c in attempt["fast_checks"]] == ["pass"]
    assert worker_head(w, w.agents.calls[0].name)["data"].get("followups") in (None, [])


# ======================================================================================
# 2. they never replace the protected verification
# ======================================================================================
def test_a_change_that_passes_the_quick_check_still_fails_the_protected_suite(
    deployment: Any, tmp_path: Path
) -> None:
    def script(call: Call) -> dict[str, str]:
        return {"app.py": GOOD if "BAD" in call.app_text else BAD_BUT_QUICK_OK}

    w = build_world(deployment, tmp_path, script, quick=True)
    trace = Trace(w)
    record = w.run(candidate(w, fast_checks=fast(w)))
    # attempt 1: quick passes, the protected suite fails -> a failed attempt, then a repair
    assert outcomes(record) == ["fail", "pass"] and record["status"] == "published"
    one, two = record["attempts"]
    assert [c["outcome"] for c in one["fast_checks"]] == ["pass"] and one["followups"] == 0
    assert one["verdicts"][0]["outcome"] != "pass"  # the verifier's verdict, not the quick check's
    assert two["verdicts"][0]["outcome"] == "pass"
    # one protected verification per attempt, each after that attempt's quick check
    assert trace.events == ["turn:first", "quick", "verify", "turn:first", "quick", "verify"]


def test_the_protected_verification_fails_what_the_follow_up_did_not_repair(
    deployment: Any, tmp_path: Path
) -> None:
    """`max_followups` 1: one follow-up, then the attempt goes to the protected verification, which
    fails the unrepaired change (the follow-up's own change is not quick-checked again)."""
    w = build_world(deployment, tmp_path, lambda call: {"app.py": THREE}, quick=True)
    trace = Trace(w)
    record = w.run(candidate(w, fast_checks=fast(w)))
    assert outcomes(record) == ["fail", "fail", "fail"] and record["status"] == "failed"
    assert trace.events[:4] == ["turn:first", "quick", "turn:resume", "verify"]
    for attempt in record["attempts"]:
        assert attempt["followups"] == 1 and [c["outcome"] for c in attempt["fast_checks"]] == [
            "fail"
        ]
        assert attempt["verdicts"][0]["outcome"] != "pass"  # the verifier decided, not the hook


def test_two_follow_ups_at_most(deployment: Any, tmp_path: Path) -> None:
    w = build_world(deployment, tmp_path, lambda call: {"app.py": THREE}, quick=True)
    trace = Trace(w)
    two = w.comps.version("fast_checks", enabled=True, checks=["quick"], max_followups=2)
    record = w.run(candidate(w, fast_checks=two))
    assert record["attempts"][0]["followups"] == 2
    assert trace.events[:6] == [
        "turn:first",
        "quick",
        "turn:resume",
        "quick",
        "turn:resume",
        "verify",
    ]
    first = w.agents.calls[0].name
    assert [c.name for c in w.agents.calls[1:3]] == [first + "-f1", first + "-f2"]
    fault(
        "COMPONENT_CONTENT",
        policies.validate_content,
        "fast_checks",
        {"enabled": True, "checks": ["quick"], "max_followups": 3},
    )


# ======================================================================================
# 3. the L7 decision
# ======================================================================================
def test_the_l7_decision_is_recorded_once_per_attempt_with_the_prior_quick_checks(
    deployment: Any, tmp_path: Path
) -> None:
    w = build_world(deployment, tmp_path, first_wrong_then_good, quick=True)
    Trace(w)
    record = w.run(candidate(w, fast_checks=fast(w)))
    goal = record["goal_id"]
    l7 = only(w, goal, "L7")  # two turns, one decision: it is made at the first turn
    assert l7["chosen"] == "quick_checks" and l7["used_prior"] is True
    assert l7["point"] == "before_final_verification" and l7["prior"] == "none"
    assert l7["features"] == {"changed_files": "1", "quick_available": True}
    assert {o["option"]: o["eligible"] for o in l7["options"]} == {
        "none": True,
        "quick_checks": True,
    }
    assert l7["decider_ref"] is None and l7["subject"] == {"goal_id": goal}


def test_an_l7_decider_may_choose_to_skip_the_quick_checks(deployment: Any, tmp_path: Path) -> None:
    w = build_world(deployment, tmp_path, quick=True)
    trace = Trace(w)
    e = env_of(w)
    method = e.method(["quick_available"], name="l7skip")
    feats = {"quick_available": True}
    data = [
        *rows("none", 20, 18, feats, tokens=100),
        *rows("quick_checks", 20, 18, feats, tokens=900),
    ]
    table = e.table(
        "L7", data, features=("quick_available",), options=("none", "quick_checks"), method=method
    )
    decider = e.decider("L7", method, table)
    record = w.run(candidate(w, fast_checks=fast(w), L7=decider))
    l7 = only(w, record["goal_id"], "L7")
    assert l7["chosen"] == "none" and l7["used_prior"] is False and l7["decider_ref"] == decider
    assert l7["table_ref"] == table and l7["prior"] == "none"
    assert trace.events == ["turn:first", "verify"]  # no quick check, no follow-up
    assert record["attempts"][0]["fast_checks"] == [] and record["attempts"][0]["followups"] == 0
    assert record["status"] == "published"  # the suite alone decided


def test_an_l7_decider_may_choose_the_quick_checks(deployment: Any, tmp_path: Path) -> None:
    w = build_world(deployment, tmp_path, first_wrong_then_good, quick=True)
    trace = Trace(w)
    e = env_of(w)
    method = e.method(["quick_available"], name="l7run")
    feats = {"quick_available": True}
    data = [
        *rows("none", 20, 8, feats, tokens=100),
        *rows("quick_checks", 20, 19, feats, tokens=150),
    ]
    table = e.table(
        "L7", data, features=("quick_available",), options=("none", "quick_checks"), method=method
    )
    decider = e.decider("L7", method, table)
    record = w.run(candidate(w, fast_checks=fast(w), L7=decider))
    l7 = only(w, record["goal_id"], "L7")
    assert l7["chosen"] == "quick_checks" and l7["used_prior"] is False
    assert trace.events == ["turn:first", "quick", "turn:resume", "verify"]


def test_without_fast_checks_the_decision_is_none_and_nothing_runs(
    deployment: Any, tmp_path: Path
) -> None:
    w = build_world(deployment, tmp_path, quick=True)
    trace = Trace(w)
    record = w.run(
        w.comps.base_ref
    )  # the v1 manifest: fast checks off though a quick verifier exists
    assert trace.events == ["turn:first", "verify"] and len(w.agents.calls) == 1
    l7 = only(w, record["goal_id"], "L7")
    assert l7["chosen"] == "none" and l7["features"] == {
        "changed_files": "1",
        "quick_available": False,
    }
    quick = next(o for o in l7["options"] if o["option"] == "quick_checks")
    assert quick["eligible"] is False and quick["why"]
    assert "fast_checks" not in record["attempts"][0]


# ======================================================================================
# 4. what the loop refuses before any claim
# ======================================================================================
def held_before_any_claim(w: World, composition: Ref, *needles: str) -> dict[str, Any]:
    goal = w.approved(composition)
    record = w.loop.run_goal(goal)
    assert record["status"] == "held" and record["attempts"] == []
    assert "COMPONENT_CONTENT" in record["reason"]
    for needle in needles:
        assert needle in record["reason"], (needle, record["reason"])
    assert w.agents.calls == [] and w.rig.published == []
    return record


def test_fast_checks_that_cannot_run_are_refused_before_any_claim(
    deployment: Any, tmp_path: Path
) -> None:
    w = build_world(deployment, tmp_path, quick=True)
    Trace(w)
    held_before_any_claim(
        w,
        candidate(
            w, fast_checks=w.comps.version("fast_checks", enabled=True, checks=[], max_followups=1)
        ),
        "enabled without checks",
    )
    held_before_any_claim(
        w,
        candidate(
            w,
            fast_checks=w.comps.version(
                "fast_checks", enabled=True, checks=["quick"], max_followups=0
            ),
        ),
        "max_followups 0",
    )
    # `suite` is a verifier of the app but not a quick one; `ghost` is no verifier at all
    for check in ("suite", "ghost"):
        held_before_any_claim(
            w,
            candidate(
                w,
                fast_checks=w.comps.version(
                    "fast_checks", enabled=True, checks=[check], max_followups=1
                ),
            ),
            "not quick verifiers",
            check,
        )


def test_fast_checks_with_a_strategy_that_holds_the_hook_are_refused(
    deployment: Any, tmp_path: Path
) -> None:
    """generator_reviewer resumes the session for its own review rounds: one M3 hook per attempt."""
    w = build_world(deployment, tmp_path, quick=True)
    execution = w.comps.version(
        "execution_strategy",
        enabled=["generator_reviewer"],
        params={"generator_reviewer": copy.deepcopy(DEFAULT_PARAMS["generator_reviewer"])},
    )
    limits = w.comps.version("limits", **{k: v for k, v in LIMIT.items() if k == "aux_max_tokens"})
    held_before_any_claim(
        w,
        candidate(w, fast_checks=fast(w), execution_strategy=execution, limits=limits),
        "generator_reviewer",
    )


def test_fast_check_content_is_validated(env: Env) -> None:
    ok = {"enabled": True, "checks": ["quick"], "max_followups": 1}
    policies.validate_content("fast_checks", ok)
    policies.validate_content("fast_checks", copy.deepcopy(policies.V1["fast_checks"]))
    for bad in (
        {**ok, "enabled": "yes"},
        {**ok, "checks": ["a", "b", "c", "d", "e"]},  # at most 4 (§2.2)
        {**ok, "checks": ["quick", "quick"]},
        {**ok, "checks": ["has space"]},
        {**ok, "max_followups": -1},
        {**ok, "max_followups": 3},
        {**ok, "max_followups": True},
        {**ok, "surprise": 1},
    ):
        fault("COMPONENT_CONTENT", policies.validate_content, "fast_checks", bad)
    assert policies.V1["fast_checks"] == {"enabled": False, "checks": [], "max_followups": 0}
    assert env.register("fast_checks", "v1", copy.deepcopy(policies.V1["fast_checks"]))


@pytest.fixture
def env(deployment: Any) -> Env:
    return Env(deployment)


def test_every_goal_records_its_l7_decision_in_order_with_the_others(
    deployment: Any, tmp_path: Path
) -> None:
    w = build_world(deployment, tmp_path, first_wrong_then_good, quick=True)
    Trace(w)
    record = w.run(candidate(w, fast_checks=fast(w)))
    layers = [d["layer"] for d in decisions_of(w, record["goal_id"])]
    assert layers == ["L1", "L2", "L3", "L8", "L5", "L4", "L7"]  # L7 after L4, before the verdict
