"""Work 033 S8 - parallel trials, the per-trial write resource (IC-03), deadlines, loop options.

Contract: specs/033-harness-taxonomy/interfaces.md IC-03, 3.3, 3.4, 8.4 and the clarification
"options wiring from the loop (resolve_options, S8)".

Real: store, runtime, worker coordinator, execution loop, workspaces, threads. Stand-in: the
scripted host-process "container" (an agent that waits for its sibling before it writes, so two
trials of one app finish only when both run at once) and a fixed planner. No docker, no provider,
no network.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from test_033_s4_binding import HIGH_ID, MODEL, Setup
from test_033_s8_executor import World, make_world

from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.execution import loop as loop_module
from amplai_foundry.runtime.execution.cells import DispatchOptions
from amplai_foundry.runtime.execution.loop import ExecutionLoop
from amplai_foundry.runtime.execution.product import (
    LocalExecutionService,
    TrialContext,
    write_resource,
)
from rc06_rig import submit

APP_RESOURCE = "sandbox:app"


def trial_context(**over: Any) -> TrialContext:
    fields: dict[str, Any] = {
        "subject": {"experiment_id": "exp-1", "trial_id": "trial-1"},
        "arm": "candidate", "cell_id": "codex-cli", "split": "development",
        "capture_trace": False, "planner_mode": "fixed", "environment_id": "app",
    }  # fmt: skip
    return TrialContext(**{**fields, **over})


def goal_nodes(service: LocalExecutionService, goal_id: str) -> list[dict[str, Any]]:
    plan = service.plan_record(goal_id)
    graph = service.store.get(service.scope, "workgraph", plan["graph_ref"])
    nodes: list[dict[str, Any]] = graph["nodes"]
    return nodes


def planned(world: World, text: str, *, trial: TrialContext | None = None) -> str:
    goal = submit(world.rig, text)
    world.rig.service.plan(goal, composition=world.baseline if trial else None, trial=trial)
    return goal


# -- the write resource (IC-03) --------------------------------------------------------------------
def test_write_resource_names_the_per_trial_claim_only_for_a_trial_goal() -> None:
    assert write_resource("app", "goal-7", trial=False) == "sandbox:app"
    assert write_resource("app", "goal-7", trial=True) == "sandbox:app:trial:goal-7"
    assert write_resource("other", "goal-7", trial=True) != write_resource(
        "app", "goal-7", trial=True
    )


def test_a_published_goal_keeps_the_app_write_claim(deployment: Any, tmp_path: Path) -> None:
    world = make_world(deployment, tmp_path)
    goal = planned(world, "published goal: make value return 2")
    (node,) = goal_nodes(world.rig.service, goal)
    assert node["resource_claims"] == [{"resource": APP_RESOURCE, "mode": "exclusive_write"}]
    assert "trial" not in world.rig.service.plan_record(goal)


def test_a_trial_goal_claims_its_own_resource_and_keeps_the_app_capabilities(
    deployment: Any, tmp_path: Path
) -> None:
    world = make_world(deployment, tmp_path)
    goal = planned(world, "trial goal: make value return 2", trial=trial_context())
    (node,) = goal_nodes(world.rig.service, goal)
    assert node["resource_claims"] == [
        {"resource": f"sandbox:app:trial:{goal}", "mode": "exclusive_write"}
    ]
    published = planned(world, "published goal two: make value return 2")
    (other,) = goal_nodes(world.rig.service, published)
    assert node["capabilities"] == other["capabilities"]  # only the claim differs
    assert world.rig.service.plan_record(goal)["trial"] == trial_context().wire()


def test_two_published_goals_conflict_on_the_app_claim_but_two_trial_goals_do_not(
    deployment: Any, tmp_path: Path
) -> None:
    world = make_world(deployment, tmp_path)
    service, worker = world.rig.service, world.rig.actors.worker

    def approved(text: str, trial: TrialContext | None) -> str:
        goal = planned(world, text, trial=trial)
        service.approve(world.rig.operator, goal)
        return goal

    first = approved("published one: make value return 2", None)
    second = approved("published two: make value return 2", None)
    assert service.runtime.claim(worker, goal_id=first) is not None
    assert service.runtime.claim(worker, goal_id=second) is None  # write concurrency 1 per repo
    # trial goals: the published goals' claim does not block them, nor do they block each other
    a = approved("trial one: make value return 2", trial_context())
    b = approved("trial two: make value return 2", trial_context())
    claim_a = service.runtime.claim(worker, goal_id=a)
    claim_b = service.runtime.claim(worker, goal_id=b)
    assert claim_a is not None and claim_b is not None  # `first` still holds sandbox:app
    assert {c["node"]["resource_claims"][0]["resource"] for c in (claim_a, claim_b)} == {
        f"sandbox:app:trial:{a}",
        f"sandbox:app:trial:{b}",
    }


def test_a_trial_context_with_a_bad_field_is_refused(deployment: Any, tmp_path: Path) -> None:
    for over in (
        {"arm": ""}, {"cell_id": ""}, {"split": ""}, {"environment_id": ""},
        {"capture_trace": "yes"}, {"planner_mode": "hybrid"}, {"write_scope": "app"},
        {"subject": {"trial_id": 3}}, {"subject": ["x"]},
    ):  # fmt: skip
        with pytest.raises(RuntimeFault) as refused:
            trial_context(**over)
        assert refused.value.code == "TRIAL_CONTEXT", over
        assert refused.value.details, over  # it names what is wrong


def test_plan_refuses_a_trial_that_is_not_a_trial_context(deployment: Any, tmp_path: Path) -> None:
    world = make_world(deployment, tmp_path)
    goal = submit(world.rig, "bad trial argument: make value return 2")
    with pytest.raises(RuntimeFault) as refused:
        world.rig.service.plan(goal, trial={"arm": "x"})  # type: ignore[arg-type]
    assert refused.value.code == "TRIAL_CONTEXT"


# -- two trials of one app at once (8.4) --
def run_in_threads(jobs: list[Any]) -> list[Any]:
    results: list[Any] = [None] * len(jobs)
    errors: list[BaseException] = []

    def target(i: int) -> None:
        try:
            results[i] = jobs[i]()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=target, args=(i,)) for i in range(len(jobs))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=240)
    assert not errors, errors
    assert not any(t.is_alive() for t in threads)
    return results


def test_two_trials_of_one_app_run_at_the_same_time(deployment: Any, tmp_path: Path) -> None:
    world = make_world(deployment, tmp_path, "rendezvous")
    world.container.rendezvous = tmp_path / "rendezvous"
    snapshots: list[set[tuple[str, str]]] = []

    def held_resources(dispatch_id: str) -> None:
        with world.store._lock:
            rows = world.store.conn.execute(
                "SELECT resource, mode FROM resources WHERE tenant=? AND project=?",
                world.scope.keys(),
            ).fetchall()
        snapshots.append({(r["resource"], r["mode"]) for r in rows})

    world.container.hooks.append(held_resources)
    # each agent writes only once both are running: a serialised pair would give exit 4 (no answer)
    first, second = run_in_threads(
        [lambda: world.run("bug-01-value", 0), lambda: world.run("bug-01-value", 1)]
    )
    assert first.success is True and second.success is True
    for obs in (first, second):
        assert (obs.safety_failures, obs.unknown_effects) == (0, 0)
    goals = {world.receipt(first)["goal_id"], world.receipt(second)["goal_id"]}
    assert len(goals) == 2
    # the two claims that were held at once are the per-trial resources, never sandbox:app
    claimed = {res for snap in snapshots for res, _ in snap}
    assert claimed == {f"sandbox:app:trial:{g}" for g in goals}
    assert any(len(snap) == 2 for snap in snapshots)  # both were held at the same moment
    assert all(mode == "exclusive_write" for snap in snapshots for _, mode in snap)
    # self.trials is kept under a lock: two complete rows, one per call
    trials = world.executor.trials
    assert len(trials) == 2 and {t["goal_id"] for t in trials} == goals
    assert sorted(t["repeat"] for t in trials) == [0, 1]
    assert world.executor._bound == set()


def test_trials_of_different_arms_run_together_and_do_not_share_a_workspace(
    deployment: Any, tmp_path: Path
) -> None:
    world = make_world(deployment, tmp_path, "rendezvous")
    world.container.rendezvous = tmp_path / "rendezvous"
    candidate = world.candidate()
    base_obs, cand_obs = run_in_threads(
        [lambda: world.run("bug-01-value", 0), lambda: world.run("bug-01-value", 0, candidate)]
    )
    assert base_obs.success is True and cand_obs.success is True
    plans = [
        world.rig.service.plan_record(world.receipt(o)["goal_id"]) for o in (base_obs, cand_obs)
    ]
    assert plans[0]["composition"]["ref"] == world.baseline
    assert plans[1]["composition"]["ref"] == candidate
    markers = sorted(p.name for p in (tmp_path / "rendezvous").iterdir())
    assert len(markers) == 2 and len(set(markers)) == 2  # two distinct workspaces


def test_a_published_goal_does_not_overlap_a_trial_of_the_same_app_claim(
    deployment: Any, tmp_path: Path
) -> None:
    """A trial never blocks on a published goal's claim and never takes it: after a trial ran, the
    app's claim is free for a published goal."""
    world = make_world(deployment, tmp_path)
    assert world.run("bug-01-value").success is True
    goal = planned(world, "published after: make value return 2")
    world.rig.service.approve(world.rig.operator, goal)
    assert world.rig.service.runtime.claim(world.rig.actors.worker, goal_id=goal) is not None


# -- a trial goal never outlives its call (the premise of IC-03: trial goals never publish) --
def plan_states(world: World) -> dict[str, str]:
    with world.store._lock:
        rows = world.store.conn.execute(
            "SELECT id, state FROM heads WHERE tenant=? AND project=? AND kind='execution-plan'",
            world.scope.keys(),
        ).fetchall()
    return {r["id"]: r["state"] for r in rows}


def serving_loop(world: World, published: list[str]) -> ExecutionLoop:
    """A later loop on the same store with a publisher (``amplai ops local-serve``)."""

    def publisher(goal_id: str) -> dict[str, Any]:
        published.append(goal_id)
        return {"branch": "amplai/" + goal_id}

    return ExecutionLoop(world.rig.service, world.coordinator, publisher=publisher)


def assert_closed(world: World, goal: str, goal_state: str) -> None:
    """Approval revoked, runtime goal ended, and a publishing loop neither requeues nor runs it."""
    plan = world.rig.service.plan_record(goal)
    assert not world.loop._approval_valid(plan)
    assert world.store.head(world.scope, "goal", goal)["state"] == goal_state
    published: list[str] = []
    serving = serving_loop(world, published)
    assert [a for a in serving.reconcile() if a["goal_id"] == goal] == []
    assert serving.next_goal() is None and published == []


def test_a_trial_whose_claim_failed_is_not_left_for_a_requeue(
    deployment: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the loop keeps the approval of a goal whose claim failed before any attempt, so reconcile
    # can requeue it (loop.py claim failure); a trial goal must not be run later by another loop
    world = make_world(deployment, tmp_path)
    service = world.rig.service
    with monkeypatch.context() as patched:
        patched.setattr(service.runtime, "claim", lambda *a, **k: None)
        obs = world.run("bug-01-value")
    assert obs.success is None
    receipt = world.receipt(obs)
    goal = receipt["goal_id"]
    assert receipt["goal_status"] == "held" and receipt["goal_reason"].startswith("claim failed")
    plan = service.plan_record(goal)
    assert plan["status"] == "held" and plan["reason"] == receipt["goal_reason"]
    assert plan["attempts"] == [] and world.container.prompts == []
    assert_closed(world, goal, "failed")


def test_an_error_after_approval_leaves_no_approved_trial_goal(
    deployment: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = make_world(deployment, tmp_path)

    def broken(goal_id: str) -> dict[str, Any]:
        raise RuntimeError("the loop broke before it started the goal")

    monkeypatch.setattr(world.loop, "run_goal", broken)
    with pytest.raises(RuntimeError):
        world.run("bug-01-value")
    ((goal, state),) = plan_states(world).items()
    assert state == "held"
    assert world.rig.service.plan_record(goal)["reason"] == "trial error: RuntimeError"
    assert_closed(world, goal, "failed")


def test_a_trial_goal_the_loop_leaves_waiting_is_cancelled(
    deployment: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = make_world(deployment, tmp_path)
    loop = world.loop

    def replan_failed(goal_id: str) -> dict[str, Any]:
        return loop._finish(goal_id, "replan_failed", reason="PLANNER: no draft", attempts=[])

    monkeypatch.setattr(loop, "run_goal", replan_failed)
    obs = world.run("bug-01-value")
    assert obs.success is None  # graded from what the loop returned
    receipt = world.receipt(obs)
    assert receipt["goal_status"] == "replan_failed"
    assert world.rig.service.plan_record(receipt["goal_id"])["status"] == "cancelled"
    assert_closed(world, receipt["goal_id"], "cancelled")


def test_a_goal_the_loop_already_stopped_is_left_as_it_is(
    deployment: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = make_world(deployment, tmp_path, "crash")  # driver_failed: the loop stops it held
    stops: list[str] = []
    original = world.loop._stop_goal

    def recording(goal_id: str, status: str, *rest: Any) -> dict[str, Any]:
        stops.append(status)
        return original(goal_id, status, *rest)

    monkeypatch.setattr(world.loop, "_stop_goal", recording)
    obs = world.run("bug-01-value")
    goal = world.receipt(obs)["goal_id"]
    plan = world.rig.service.plan_record(goal)
    assert plan["status"] == "held" and plan["reason"].startswith("driver failed")
    assert stops == ["held"]  # stopped once, by the loop; the executor did not stop it again
    assert_closed(world, goal, "failed")


# -- deadlines --
@pytest.mark.parametrize("bad", [0, -1, -0.5, True, False, "30", float("nan"), object(), [5]])
def test_a_call_deadline_must_be_a_positive_number(
    deployment: Any, tmp_path: Path, bad: Any
) -> None:
    world = make_world(deployment, tmp_path)
    worker = world.rig.actors.worker
    with pytest.raises(RuntimeFault) as refused:
        world.coordinator.execute(
            worker, {}, prompt="x", base_snapshot={}, output_paths={}, deadline_seconds=bad
        )
    assert refused.value.code == "WORKER_LIMITS"
    with pytest.raises(RuntimeFault) as refused_resume:
        world.coordinator.continue_resumed(worker, "run-none", deadline_seconds=bad)
    assert refused_resume.value.code == "WORKER_LIMITS"


def test_the_call_limit_is_its_deadline_else_max_seconds(deployment: Any, tmp_path: Path) -> None:
    world = make_world(deployment, tmp_path)
    coordinator = world.coordinator
    coordinator.max_seconds = 123
    assert coordinator._limit(None) == 123
    assert coordinator._limit(7) == 7.0 and coordinator._limit(0.25) == 0.25
    assert coordinator.max_seconds == 123  # a call deadline never writes the shared field


def test_the_loop_passes_the_remaining_budget_per_call_and_never_writes_max_seconds(
    deployment: Any, tmp_path: Path
) -> None:
    world = make_world(deployment, tmp_path)
    coordinator = world.coordinator
    coordinator.max_seconds = 4321
    calls: list[dict[str, Any]] = []
    original = coordinator.execute

    def recording(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return original(*args, **kwargs)

    coordinator.execute = recording  # type: ignore[method-assign]
    obs = world.run("bug-01-value")
    assert obs.success is True
    assert coordinator.max_seconds == 4321  # the loop no longer mutates the coordinator
    (call,) = calls
    deadline = call["deadline_seconds"]
    assert type(deadline) is int and deadline >= 1
    plan = world.rig.service.plan_record(world.receipt(obs)["goal_id"])
    contract = world.store.get(world.scope, "goal-contract", plan["contract_ref"])
    assert deadline <= contract["budget"]["max_wall_seconds"]  # the remaining goal wall budget
    assert isinstance(call["options"], DispatchOptions)


def test_a_call_deadline_replaces_a_tiny_shared_max_seconds(
    deployment: Any, tmp_path: Path
) -> None:
    world = make_world(deployment, tmp_path)
    world.coordinator.max_seconds = 0.001  # the old loop wrote this field; a call would time out
    assert world.run("bug-01-value").success is True


def test_the_remaining_wall_budget_stops_a_slow_attempt(deployment: Any, tmp_path: Path) -> None:
    world = make_world(deployment, tmp_path, "sleep")
    goal = planned(world, "slow attempt: make value return 2")
    service = world.rig.service
    service.approve(world.rig.operator, goal)
    plan = service.plan_record(goal)
    contract = world.store.get(world.scope, "goal-contract", plan["contract_ref"])
    end = loop_module._utc(plan["approved_at"]) + contract["budget"]["max_wall_seconds"]
    world.loop.clock = lambda: end - 2.5  # 2 seconds of the goal's wall budget remain
    record = world.loop.run_goal(goal)
    assert record["status"] == "held" and "WORKER_DEADLINE" in record["reason"]
    assert [a["outcome"] for a in record["attempts"]] == ["driver_failed"]
    assert record["attempts"][0]["reason"] == "WORKER_DEADLINE"


def test_a_goal_with_no_wall_budget_left_is_timed_out_before_any_claim(
    deployment: Any, tmp_path: Path
) -> None:
    world = make_world(deployment, tmp_path)
    goal = planned(world, "no time left: make value return 2")
    world.rig.service.approve(world.rig.operator, goal)
    plan = world.rig.service.plan_record(goal)
    contract = world.store.get(world.scope, "goal-contract", plan["contract_ref"])
    end = loop_module._utc(plan["approved_at"]) + contract["budget"]["max_wall_seconds"]
    world.loop.clock = lambda: end + 1
    record = world.loop.run_goal(goal)
    assert record["status"] == "timed_out" and world.container.prompts == []


# -- the loop resolves the goal's dispatch options (clarification: cells.resolve_options) --
@pytest.fixture
def setup(deployment: Any, tmp_path: Path) -> Setup:
    return Setup(deployment, tmp_path)


def record_options(setup: Setup) -> list[DispatchOptions | None]:
    seen: list[DispatchOptions | None] = []
    original = setup.coordinator.execute

    def recording(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("options"))
        return original(*args, **kwargs)

    setup.coordinator.execute = recording  # type: ignore[method-assign]
    return seen


def test_an_effort_profile_runs_with_its_effort_and_is_not_held(setup: Setup) -> None:
    seen = record_options(setup)
    composition = setup.rig.service.apps["app"].compositions[HIGH_ID]
    record = setup.loop.run_goal(setup.goal(composition))  # nothing wraps the options any more
    assert record["status"] == "published", record.get("reason")
    assert "DISPATCH_OPTIONS_BINDING" not in str(record.get("reason"))
    (argv,) = setup.container.argvs
    assert argv[argv.index("-c") : argv.index("-c") + 2] == ["-c", "model_reasoning_effort=high"]
    assert seen == [DispatchOptions(MODEL, "high", capture_trace=False)]


def test_a_legacy_goal_resolves_the_provider_default(setup: Setup) -> None:
    seen = record_options(setup)
    assert setup.loop.run_goal(setup.goal())["status"] == "published"
    assert seen == [DispatchOptions.default(MODEL)]
    assert "-c" not in setup.container.argvs[0]


def test_the_trial_trace_flag_reaches_the_options_only_for_a_trial_goal(setup: Setup) -> None:
    seen = record_options(setup)
    service, rig = setup.rig.service, setup.rig
    trials = ExecutionLoop(service, setup.coordinator, publisher=None)  # trials never publish
    composition = service.apps["app"].compositions[HIGH_ID]
    context = trial_context(cell_id=HIGH_ID, capture_trace=True)
    goal = submit(rig, "traced trial: make value return 2")
    service.plan(goal, composition=composition, trial=context)
    service.approve(rig.operator, goal)
    assert trials.run_goal(goal)["status"] == "verified"
    assert seen[-1] is not None and seen[-1].capture_trace is True and seen[-1].effort == "high"
    plain = setup.goal(composition)
    setup.loop.run_goal(plain)
    assert seen[-1] is not None and seen[-1].capture_trace is False  # a real goal never captures
    untraced = submit(rig, "untraced trial: make value return 2")
    service.plan(untraced, composition=composition, trial=trial_context(cell_id=HIGH_ID))
    service.approve(rig.operator, untraced)
    assert trials.run_goal(untraced)["status"] == "verified"
    assert seen[-1] is not None and seen[-1].capture_trace is False
    assert setup.published_goals == [plain]  # only the real goal went to the publisher


def test_options_that_cannot_be_resolved_hold_the_goal_before_any_claim(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise Hold("DRIVER_OPTIONS_UNSUPPORTED", "Driver options for another driver")

    monkeypatch.setattr(loop_module, "resolve_options", refuse)
    record = setup.loop.run_goal(setup.goal())
    assert record["status"] == "held"
    assert record["reason"].startswith("dispatch options: DRIVER_OPTIONS_UNSUPPORTED")
    assert setup.container.prompts == [] and setup.published_goals == []
    assert record["attempts"] == []  # nothing was claimed


@pytest.mark.parametrize(
    ("options", "on_high"),
    [
        (DispatchOptions(MODEL, "high"), False),  # an effort the legacy profile does not have
        (DispatchOptions(MODEL, "low"), True),  # another effort than the profile's
        (DispatchOptions.default(MODEL), True),  # no effort for an effort profile
        (DispatchOptions("another-model", "high"), True),  # another model
    ],
)
def test_resolved_options_that_differ_from_the_profile_hold_before_anything_runs(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, options: DispatchOptions, on_high: bool
) -> None:
    # the loop now resolves the options itself; the worker still checks the binding (S4)
    monkeypatch.setattr(loop_module, "resolve_options", lambda *a, **k: options)
    composition = setup.rig.service.apps["app"].compositions[HIGH_ID] if on_high else None
    record = setup.loop.run_goal(setup.goal(composition))
    assert record["status"] == "held" and "DISPATCH_OPTIONS_BINDING" in record["reason"]
    assert setup.container.argvs == [] and setup.container.prompts == []  # nothing was prepared
    assert setup.published_goals == []


def test_an_effort_profile_dispatched_without_options_holds_at_the_worker(setup: Setup) -> None:
    # the loop always passes options now; a caller that passes none still cannot run the profile's
    # model at the provider default (never substituted, spec.md Constraints)
    rig = setup.rig
    goal = setup.goal(rig.service.apps["app"].compositions[HIGH_ID])
    dispatch = rig.service.runtime.claim(rig.actors.worker, goal_id=goal)
    assert dispatch is not None
    with pytest.raises(Hold) as held:
        setup.coordinator.execute(
            rig.actors.worker, dispatch, prompt="x",
            base_snapshot=rig.service.plan_record(goal)["base"],
            output_paths={loop_module.PORT: loop_module.PATCH_BINDING}, options=None,
        )  # fmt: skip
    assert held.value.code == "DISPATCH_OPTIONS_BINDING"
    assert setup.container.argvs == []


def test_an_effort_profile_on_a_port_without_options_is_held_not_substituted(
    deployment: Any, tmp_path: Path
) -> None:
    setup = Setup(deployment, tmp_path, plain_port=True)
    composition = setup.rig.service.apps["app"].compositions[HIGH_ID]
    record = setup.loop.run_goal(setup.goal(composition))
    assert record["status"] == "held" and "DRIVER_OPTIONS_UNSUPPORTED" in record["reason"]
    assert setup.container.argvs == []


# -- the loop's own trial guards (IC-03, orchestrator review 2026-10-01) --------------------------
def test_a_serving_loop_never_picks_an_approved_trial_goal(deployment: Any, tmp_path: Path) -> None:
    # a trial goal left approved (e.g. by a crash between the executor's approve and run_goal) is
    # never run by a publishing loop; a real goal approved after it still is
    world = make_world(deployment, tmp_path)
    service = world.rig.service
    trial = planned(world, "trial left approved: make value return 2", trial=trial_context())
    service.approve(world.rig.operator, trial)
    real = planned(world, "real goal: make value return 2")
    service.approve(world.rig.operator, real)
    published: list[str] = []
    serving = serving_loop(world, published)
    assert service.plan_record(trial)["status"] == "approved"
    assert serving.next_goal() == real


def test_a_verified_trial_goal_is_never_published_even_by_a_loop_with_a_publisher(
    deployment: Any, tmp_path: Path
) -> None:
    # the executor is built on a loop without a publisher; if the loop gains one later, the loop's
    # own check of plan["trial"] still keeps the verified trial goal from publishing
    world = make_world(deployment, tmp_path)
    published: list[str] = []

    def publisher(goal_id: str) -> dict[str, Any]:
        published.append(goal_id)
        return {"branch": "amplai/" + goal_id}

    world.loop.publisher = publisher
    obs = world.run("bug-01-value")
    assert obs.success is True
    goal = world.receipt(obs)["goal_id"]
    assert world.rig.service.plan_record(goal)["status"] == "verified"
    assert published == []
