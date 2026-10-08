"""Work 033 S13: trace capture, admission, storage and the proposer-only ACL
(interfaces.md §2.11, §3.12, §9.1, §9.3, D-100, AC-07 unit part).

Real: store, CAS, ``TraceService``, ``WorkCoordinator``, ``CliDriver`` (host-process stand-in for
the container, as the rc06 rig), execution loop and ``ReadOnlyTurn`` sanitization. Stand-ins: the
scripted agent that prints Codex events, fixture heads for the trials a trace names. No driver,
docker or network is used.

Traces exist only for trial goals of the corpus (``TrialContext.capture_trace``); a real goal never
captures. A secret in a trace is ``trace-drop`` (nothing stored). A proposer reads development
traces only (``Hold TRACE_ACL``); traces are never exported.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_033_s4_binding import HIGH_ID, Setup
from test_033_s11_stages import World, build_world, fault, hold
from test_033_s13_sanitizer import LONG_KEY, across_cut, claude_assistant, codex_message

from amplai_foundry.evaluation.telemetry import project_event
from amplai_foundry.meta_harness import traces
from amplai_foundry.meta_harness.traces import (
    DROP_KIND,
    SANITIZER_VERSION,
    TRACE_KIND,
    TraceBuffer,
    TraceService,
    combine,
    trace_sink,
)
from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.execution import readonly_turn
from amplai_foundry.runtime.execution.cells import DispatchOptions
from amplai_foundry.runtime.execution.loop import ExecutionLoop
from amplai_foundry.runtime.execution.meta_local import PROPOSER_PERMISSIONS
from amplai_foundry.runtime.execution.product import TrialContext
from amplai_foundry.runtime.execution.worker import WorkCoordinator, port_trace
from amplai_foundry.runtime.storage.store import Scope
from rc06_rig import AGENT, submit

CELL = "codex-cli"
SECRET = "password: Zq9wXk3LmN8vBc2RtY6uHj4P"
MARK = "UNIQUE-TRACE-TEXT-7731"


@pytest.fixture
def w(tmp_path: Path):  # type: ignore[no-untyped-def]
    with build_world(tmp_path) as world:
        yield world


# -- helpers ---------------------------------------------------------------------------------------
def message(text: str, kind: str = "message") -> dict[str, Any]:
    return {"type": kind, "role": "assistant", "text": text, "tool": None, "exit_code": None}


def sanitized(*texts: str, driver: str = "codex-cli") -> dict[str, Any]:
    """A ``combine`` value with one turn holding these messages."""
    snap = {
        **TraceBuffer("codex").snapshot(),
        "items": [message(t) for t in texts],
        "events": len(texts),
    }
    return combine(driver, [(0, snap)])


def context(
    *,
    trial_id: str = "trial-1",
    split: str = "development",
    cell: str = CELL,
    capture: bool = True,
    arm: str = "candidate",
    subject: dict[str, str] | None = None,
) -> TrialContext:
    return TrialContext(
        subject=subject or {"experiment_id": "exp-1", "trial_id": trial_id},
        arm=arm,
        cell_id=cell,
        split=split,
        capture_trace=capture,
        planner_mode="fixed",
        environment_id="app",
    )


def trial_head(
    w: World, trial_id: str, task_id: str = "bug-dev-00", kind: str = "eval-trial"
) -> None:
    """The dispatching trial head ``TraceService`` reads the task from (written before the
    executor is called)."""
    with w.store.tx() as db:
        try:
            w.store.head(w.scope, kind, trial_id, db=db)
        except RuntimeFault:
            w.store.cas(db, w.scope, kind, trial_id, 0, "running", {"task_id": task_id})


def admit(
    w: World,
    run_id: str,
    *,
    split: str = "development",
    cell: str = CELL,
    task: str = "bug-dev-00",
    texts: tuple[str, ...] = ("hello",),
    capture: bool = True,
) -> Any:
    trial_id = "trial-" + run_id
    trial_head(w, trial_id, task)
    return TraceService(w.store, w.scope, w.artifacts).admit(
        run_id=run_id,
        goal_id="goal-" + run_id,
        trial=context(trial_id=trial_id, split=split, cell=cell, capture=capture),
        driver_id="codex-cli",
        sanitized=sanitized(*texts),
    )


def service(w: World) -> TraceService:
    return TraceService(w.store, w.scope, w.artifacts)


def proposer(w: World) -> Actor:
    return w.local.proposer


def drop_of(w: World, run_id: str) -> dict[str, Any]:
    return next(v for r, v in w.objects(DROP_KIND) if r["id"] == "tracedrop-" + run_id)


# ==================================================================================================
# admission: only trial goals with capture_trace, sanitized, restricted
# ==================================================================================================
def test_a_captured_trial_is_stored_as_a_trace_record_and_a_restricted_artifact(w: World) -> None:
    ref = admit(w, "run-a", texts=("I fix add().", "Done."))
    assert ref is not None and ref["id"] == "trace-run-a" and ref["revision"] == 1
    record = w.store.get(w.scope, TRACE_KIND, ref)
    assert record["schema"] == "amplai.harness-trace.v1" and record["scope"] == w.scope.wire()
    assert (record["run_id"], record["goal_id"]) == ("run-a", "goal-run-a")
    assert (record["task_id"], record["split"], record["arm"]) == (
        "bug-dev-00", "development", "candidate",
    )  # fmt: skip
    assert (record["cell_id"], record["driver_id"], record["experiment_id"]) == (
        CELL, "codex-cli", "exp-1",
    )  # fmt: skip
    assert record["trial_ref"] is None  # the trial record is written after the executor returns
    assert record["sanitizer_version"] == SANITIZER_VERSION == "trace-sanitizer-v1"
    assert record["events"] == 2 and record["dropped_event_types"] == {}
    artifact = record["artifact"]
    assert set(artifact) == {"id", "digest", "media_type", "size_bytes"}
    assert artifact["media_type"] == "application/json"
    with w.store._lock:  # classification and trust of the CAS row
        row = w.store.conn.execute(
            "SELECT classification, trust FROM artifacts WHERE id=?", (artifact["id"],)
        ).fetchone()
    assert (row[0], row[1]) == ("restricted", "worker")
    body = json.loads(w.artifacts.read(w.scope, artifact))
    assert body == {
        "run_id": "run-a",
        "turns": [
            {
                "turn": 0,
                "items": [message("I fix add().", "message"), message("Done.")],
                "result": None,
            }
        ],
    }


def test_a_trial_without_capture_trace_is_never_stored(w: World) -> None:
    assert admit(w, "run-nocap", capture=False) is None
    assert drop_of(w, "run-nocap")["reason"] == "not_trial"
    assert w.objects(TRACE_KIND) == []


def test_a_real_goal_has_no_trial_context_and_is_never_stored(w: World) -> None:
    ref = service(w).admit(
        run_id="run-real", goal_id="goal-real", trial=None, driver_id="codex-cli",
        sanitized=sanitized("a real goal"),
    )  # fmt: skip
    assert ref is None
    assert drop_of(w, "run-real") == {
        **drop_of(w, "run-real"), "reason": "not_trial", "patterns": [], "run_id": "run-real",
    }  # fmt: skip
    assert w.objects(TRACE_KIND) == []


@pytest.mark.parametrize(
    "trial",
    [
        {"subject": {"trial_id": "t"}},  # missing fields: TrialContext refuses it
        "not a trial",
        {},
    ],
)
def test_an_invalid_trial_context_is_not_a_trial(w: World, trial: Any) -> None:
    ref = service(w).admit(
        run_id="run-bad", goal_id="g", trial=trial, driver_id="d", sanitized=sanitized("x")
    )
    assert ref is None and drop_of(w, "run-bad")["reason"] == "not_trial"


def test_a_trial_whose_record_is_not_written_is_not_a_trial(w: World) -> None:
    """No ``eval-trial`` / ``calibration-trial`` head names the task: nothing to bind a split to."""
    ref = service(w).admit(
        run_id="run-nohead", goal_id="g", trial=context(trial_id="never-written"),
        driver_id="d", sanitized=sanitized("x"),
    )  # fmt: skip
    assert ref is None and drop_of(w, "run-nohead")["reason"] == "not_trial"


def test_an_unknown_split_is_not_a_trial(w: World) -> None:
    trial_head(w, "trial-split")
    ref = service(w).admit(
        run_id="run-split", goal_id="g", trial=context(trial_id="trial-split", split="staging"),
        driver_id="d", sanitized=sanitized("x"),
    )  # fmt: skip
    assert ref is None and drop_of(w, "run-split")["reason"] == "not_trial"


def test_a_calibration_trial_names_its_task_through_the_calibration_head(w: World) -> None:
    trial_head(w, "cal-trial-1", "feature-dev-03", kind="calibration-trial")
    ref = service(w).admit(
        run_id="run-cal", goal_id="g",
        trial=context(subject={"calibration_plan_id": "calplan-1", "trial_id": "cal-trial-1"}),
        driver_id="codex-cli", sanitized=sanitized("calibration run"),
    )  # fmt: skip
    assert ref is not None
    record = w.store.get(w.scope, TRACE_KIND, ref)
    assert record["task_id"] == "feature-dev-03" and record["experiment_id"] is None


# -- sanitized, secrets -> trace-drop -----------------------------------------------------------
def test_a_secret_in_a_trace_writes_trace_drop_and_stores_nothing(w: World) -> None:
    artifacts_before = len(list((w.store.root / "artifacts").rglob("*")))
    ref = admit(w, "run-secret", texts=("fine", "the " + SECRET))
    assert ref is None
    drop = drop_of(w, "run-secret")
    assert drop["reason"] == "secret_pattern" and drop["patterns"] == ["secret-pattern-2"]
    assert w.objects(TRACE_KIND) == []
    assert len(list((w.store.root / "artifacts").rglob("*"))) == artifacts_before  # no bytes either
    assert "Zq9wXk3L" not in json.dumps(drop)  # the drop record never holds the secret


def test_a_quoted_secret_hidden_by_json_escaping_is_still_dropped(w: World) -> None:
    ref = admit(w, "run-quoted", texts=('password = "Zq9wXk3LmN8vBc2RtY6uHj4P"',))
    assert ref is None and drop_of(w, "run-quoted")["reason"] == "secret_pattern"


def test_a_private_key_and_a_provider_key_give_their_pattern_ids(w: World) -> None:
    admit(w, "run-pem", texts=("-----BEGIN PRIVATE KEY-----",))
    admit(w, "run-ant", texts=("use sk-ant-api03-A1b2C3d4E5f6G7h8I9j0K1l2M3n4",))
    assert drop_of(w, "run-pem")["patterns"] == ["secret-pattern-1"]
    assert drop_of(w, "run-ant")["patterns"] == ["secret-pattern-3"]


def test_the_cas_secret_refusal_is_also_a_trace_drop(w: World, monkeypatch: Any) -> None:
    """If ``secret_patterns`` missed it, the CAS refuses the artifact (SECRET_DETECTED); the
    service turns that Hold into ``trace-drop`` instead of failing the run."""
    monkeypatch.setattr(traces, "secret_patterns", lambda body: [])
    ref = admit(w, "run-cas", texts=("api_key = Zq9wXk3LmN8vBc2RtY6uHj4PzzzzzzzzXXXXXX",))
    assert ref is None
    drop = drop_of(w, "run-cas")
    assert drop["reason"] == "secret_pattern" and drop["patterns"]
    assert w.objects(TRACE_KIND) == []


@pytest.mark.parametrize("side", ["head", "tail"])
@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_a_secret_across_the_cut_point_writes_trace_drop(
    w: World, provider: str, side: str
) -> None:
    """D-100: the scan runs before the cut. A key cut so that neither kept side matches a pattern
    is still a ``trace-drop`` ``secret_pattern``, and no part of it is stored."""
    text = across_cut(side)
    event = (
        codex_message(text)
        if provider == "codex"
        else claude_assistant({"type": "text", "text": text})
    )
    buffer = TraceBuffer(provider)
    buffer.add(event)
    run_id = f"run-cut-{provider}-{side}"
    trial_head(w, "trial-" + run_id)
    artifacts_before = len(list((w.store.root / "artifacts").rglob("*")))
    ref = service(w).admit(
        run_id=run_id, goal_id="g", trial=context(trial_id="trial-" + run_id),
        driver_id=f"{provider}-cli", sanitized=combine(f"{provider}-cli", [(0, buffer.snapshot())]),
    )  # fmt: skip
    assert ref is None
    drop = drop_of(w, run_id)
    assert drop["reason"] == "secret_pattern" and drop["patterns"] == ["secret-pattern-3"]
    assert w.objects(TRACE_KIND) == []
    assert len(list((w.store.root / "artifacts").rglob("*"))) == artifacts_before
    assert LONG_KEY[13:20] not in json.dumps(drop) and LONG_KEY[-20:] not in json.dumps(drop)


def test_a_secret_across_the_cut_and_an_intact_one_give_both_pattern_ids(w: World) -> None:
    buffer = TraceBuffer("codex")
    buffer.add(codex_message(across_cut("head")))
    buffer.add(codex_message("-----BEGIN PRIVATE KEY-----"))
    trial_head(w, "trial-both")
    ref = service(w).admit(
        run_id="run-both", goal_id="g", trial=context(trial_id="trial-both"),
        driver_id="codex-cli", sanitized=combine("codex-cli", [(0, buffer.snapshot())]),
    )  # fmt: skip
    assert ref is None
    assert drop_of(w, "run-both")["patterns"] == ["secret-pattern-1", "secret-pattern-3"]


def test_an_oversized_trace_is_dropped_with_reason_size(w: World) -> None:
    """Messages are never cut by the size rule (older tool outputs go first): too large -> drop."""
    huge = tuple("m" * 4000 for _ in range(80))
    assert admit(w, "run-big", texts=huge) is None
    assert drop_of(w, "run-big")["reason"] == "size"
    assert w.objects(TRACE_KIND) == []


def test_an_overflowed_buffer_is_dropped_with_reason_size(w: World) -> None:
    trial_head(w, "trial-ov")
    value = sanitized("kept so far")
    value["overflow"] = True
    ref = service(w).admit(
        run_id="run-ov", goal_id="g", trial=context(trial_id="trial-ov"), driver_id="d",
        sanitized=value,
    )  # fmt: skip
    assert ref is None and drop_of(w, "run-ov")["reason"] == "size"


@pytest.mark.parametrize("breakage", ["errors", "shape"])
def test_a_sanitizer_error_is_dropped_with_reason_sanitizer_error(w: World, breakage: str) -> None:
    trial_head(w, "trial-se")
    value = sanitized("x")
    if breakage == "errors":
        value["errors"] = 1
    else:
        value["turns"][0]["items"][0]["type"] = "reasoning"  # a type the sanitizer never emits
    ref = service(w).admit(
        run_id="run-se", goal_id="g", trial=context(trial_id="trial-se"), driver_id="d",
        sanitized=value,
    )  # fmt: skip
    assert ref is None and drop_of(w, "run-se")["reason"] == "sanitizer_error"
    assert w.objects(TRACE_KIND) == []


def test_admission_is_idempotent_per_run(w: World) -> None:
    first = admit(w, "run-idem", texts=("once",))
    again = admit(w, "run-idem", texts=("twice",))
    assert again == first and len(w.objects(TRACE_KIND)) == 1
    assert admit(w, "run-idem-drop", capture=False) is None
    assert admit(w, "run-idem-drop", capture=False) is None
    assert len([r for r, _ in w.objects(DROP_KIND) if r["id"] == "tracedrop-run-idem-drop"]) == 1


def test_the_stored_body_holds_no_dropped_material(w: World) -> None:
    buffer = TraceBuffer("codex")
    buffer.add({"type": "item.completed", "item": {"type": "agent_message", "text": "kept"}})
    buffer.add({"type": "item.completed", "item": {"type": "reasoning", "text": MARK}})
    buffer.add({"type": "item.completed", "item": {"type": "command_execution", "command": MARK}})
    trial_head(w, "trial-san")
    ref = service(w).admit(
        run_id="run-san", goal_id="g", trial=context(trial_id="trial-san"), driver_id="codex-cli",
        sanitized=combine("codex-cli", [(0, buffer.snapshot())]),
    )  # fmt: skip
    record = w.store.get(w.scope, TRACE_KIND, ref)
    assert record["dropped_event_types"] == {
        "item.completed/reasoning": 1, "item.completed/command_execution": 1,
    }  # fmt: skip
    stored = w.artifacts.read(w.scope, record["artifact"]).decode()
    assert MARK not in stored and "kept" in stored
    assert MARK not in json.dumps(record)


# ==================================================================================================
# the ACL (§9.3): the proposer reads development only; the operator reads all
# ==================================================================================================
def test_a_proposer_lists_and_reads_development_traces(w: World) -> None:
    ref = admit(w, "run-dev", texts=(MARK,))
    refs = service(w).list(proposer(w), cell_id=CELL)
    assert refs == [ref]
    read = service(w).read(proposer(w), ref)
    assert read["trace_ref"] == ref and read["record"]["split"] == "development"
    assert MARK in json.dumps(read["body"])


@pytest.mark.parametrize("split", ["validation", "holdout"])
def test_a_proposer_listing_another_split_holds_trace_acl(w: World, split: str) -> None:
    admit(w, f"run-{split}", split=split)
    hold("TRACE_ACL", service(w).list, proposer(w), cell_id=CELL, split=split)


@pytest.mark.parametrize("split", ["validation", "holdout"])
def test_a_proposer_reading_a_non_development_trace_holds_trace_acl(w: World, split: str) -> None:
    ref = admit(w, f"run-r-{split}", split=split, texts=(MARK,))
    assert ref is not None
    exc = hold("TRACE_ACL", service(w).read, proposer(w), ref)
    assert MARK not in json.dumps(str(exc.details)) and MARK not in exc.message


def test_the_operator_reads_every_split(w: World) -> None:
    refs = {s: admit(w, f"run-op-{s}", split=s) for s in ("development", "validation", "holdout")}
    for split, ref in refs.items():
        read = service(w).read(w.operator, ref)
        assert read["record"]["split"] == split
        assert service(w).list(w.operator, cell_id=CELL, split=split) == [ref]


def test_holdout_needs_the_holdout_permission_beyond_corpus_read(w: World) -> None:
    ref = admit(w, "run-ho", split="holdout")
    reader = replace(w.operator, permissions=frozenset({"corpus.read"}))
    assert service(w).list(reader, cell_id=CELL, split="validation") == []
    fault("FORBIDDEN", service(w).list, reader, cell_id=CELL, split="holdout")
    fault("FORBIDDEN", service(w).read, reader, ref)


def test_an_actor_without_corpus_read_or_propose_is_forbidden(w: World) -> None:
    ref = admit(w, "run-nobody")
    nobody = Actor("nobody", w.scope, frozenset(), "service", "x")
    fault("FORBIDDEN", service(w).list, nobody, cell_id=CELL)
    fault("FORBIDDEN", service(w).read, nobody, ref)


def test_an_actor_of_another_scope_is_forbidden(w: World) -> None:
    ref = admit(w, "run-scope")
    other = Actor("p", Scope("other-tenant", "other-project"), PROPOSER_PERMISSIONS, "service", "x")
    fault("FORBIDDEN", service(w).list, other, cell_id=CELL)
    fault("FORBIDDEN", service(w).read, other, ref)


def test_an_unknown_split_is_a_runtime_fault_for_every_reader(w: World) -> None:
    fault("CORPUS_SPLIT", service(w).list, w.operator, cell_id=CELL, split="staging")
    fault("CORPUS_SPLIT", service(w).list, proposer(w), cell_id=CELL, split="staging")


def test_list_filters_by_cell_and_orders_newest_first(w: World) -> None:
    first = admit(w, "run-l1")
    other = admit(w, "run-l-other", cell="claude-cli")
    second = admit(w, "run-l2")
    refs = service(w).list(proposer(w), cell_id=CELL)
    assert set(map(lambda r: r["id"], refs)) == {first["id"], second["id"]}
    stamps = [w.store.get(w.scope, TRACE_KIND, r)["captured_at"] for r in refs]
    assert stamps == sorted(stamps, reverse=True)
    assert service(w).list(proposer(w), cell_id="claude-cli") == [other]
    assert service(w).list(proposer(w), cell_id="no-such-cell") == []


def test_a_trace_of_a_proposer_never_reads_through_a_forged_ref_of_another_split(w: World) -> None:
    """The ACL looks at the stored record's split, not at anything the caller passes."""
    ref = admit(w, "run-forge", split="validation")
    forged = dict(ref)
    hold("TRACE_ACL", service(w).read, proposer(w), forged)


def test_counts_hold_numbers_only_never_text(w: World) -> None:
    admit(w, "run-c1", texts=(MARK,))
    admit(w, "run-c2", split="validation", texts=(MARK,))
    admit(w, "run-c3", capture=False)
    admit(w, "run-c4", texts=(SECRET,))
    counts = service(w).counts()
    assert counts == {
        "traces": {"development": 1, "validation": 1},
        "drops": {"not_trial": 1, "secret_pattern": 1},
    }
    assert MARK not in json.dumps(counts)


# ==================================================================================================
# never exported (§9.3, D-100)
# ==================================================================================================
def test_traces_are_not_in_the_telemetry_export(w: World) -> None:
    from amplai_foundry.evaluation.observatory import Observatory

    admit(w, "run-exp", texts=(MARK, "second line"))
    events = w.store.events(w.scope, after=0, limit=1000)
    assert events  # the trace puts did emit events
    projected = [project_event(w.scope, e) for e in events]
    assert MARK not in json.dumps(projected) and "second line" not in json.dumps(projected)
    exported: list[Any] = []

    def exporter(batch: list[dict[str, Any]]) -> bool:
        exported.extend(batch)
        return True

    observatory = Observatory(w.store)
    observatory.export_batch(w.scope, 0, exporter, limit=1000)
    assert exported and MARK not in json.dumps(exported)


def test_the_stored_trace_is_in_the_cas_only_not_in_any_record_value(w: World) -> None:
    admit(w, "run-cas-only", texts=(MARK,))
    for kind in (TRACE_KIND, DROP_KIND):
        for _ref, value in w.objects(kind):
            assert MARK not in json.dumps(value)


# ==================================================================================================
# the sink of a deployment: the run's goal plan decides (§9.1)
# ==================================================================================================
def put_run(w: World, run_id: str, goal_id: str) -> None:
    with w.store.tx() as db:
        w.store.cas(db, w.scope, "run", run_id, 0, "running", {"record": {"root_goal_id": goal_id}})


def test_the_sink_admits_a_run_whose_plan_carries_a_capturing_trial(w: World) -> None:
    trial_head(w, "trial-sink", "refactor-dev-02")
    put_run(w, "run-sink", "goal-sink")
    plan = {"trial": context(trial_id="trial-sink").wire()}
    sink = trace_sink(w.store, w.scope, w.artifacts, lambda goal: plan)
    sink("run-sink", sanitized("sunk"))
    record = w.store.get(w.scope, TRACE_KIND, service(w).records()[0][0])
    assert record["run_id"] == "run-sink" and record["goal_id"] == "goal-sink"
    assert record["task_id"] == "refactor-dev-02" and record["driver_id"] == "codex-cli"


def test_the_sink_drops_a_run_whose_plan_has_no_trial(w: World) -> None:
    put_run(w, "run-real-sink", "goal-real")
    sink = trace_sink(w.store, w.scope, w.artifacts, lambda goal: {"graph_ref": "x"})
    sink("run-real-sink", sanitized("a real goal's text"))
    assert w.objects(TRACE_KIND) == []
    assert drop_of(w, "run-real-sink")["reason"] == "not_trial"


def test_the_sink_drops_a_run_without_a_run_record(w: World) -> None:
    sink = trace_sink(w.store, w.scope, w.artifacts, lambda goal: {"trial": context().wire()})
    sink("run-ghost", sanitized("x"))
    assert w.objects(TRACE_KIND) == []
    assert drop_of(w, "run-ghost")["reason"] == "not_trial"


def test_the_sink_is_not_built_when_trace_capture_is_off() -> None:
    from amplai_foundry.runtime.local_deployment import LocalProductDeployment

    off = SimpleNamespace(meta=SimpleNamespace(trace_capture=False))
    assert LocalProductDeployment._trace_sink(SimpleNamespace(), off) is None  # type: ignore[arg-type]


def test_trace_capture_off_gives_the_executor_no_trace_service(w: World) -> None:
    from amplai_foundry.runtime.meta_cli import trial_traces

    off = SimpleNamespace(config=SimpleNamespace(meta=SimpleNamespace(trace_capture=False)))
    assert trial_traces(off) is None
    on = SimpleNamespace(
        config=SimpleNamespace(meta=SimpleNamespace(trace_capture=True)),
        store=w.store, scope=w.scope, artifacts=w.artifacts,
    )  # fmt: skip
    assert isinstance(trial_traces(on), TraceService)
    absent = SimpleNamespace(
        config=SimpleNamespace(), store=w.store, scope=w.scope, artifacts=w.artifacts
    )
    assert isinstance(trial_traces(absent), TraceService)  # absent meta keeps the default


# ==================================================================================================
# the coordinator: capture points (§9.1, §3.12)
# ==================================================================================================
class TracePort:
    driver_id = "codex-cli"

    def __init__(self, traces_by_handle: dict[str, Any]) -> None:
        self.by_handle = traces_by_handle

    def trace(self, handle: str) -> Any:
        return self.by_handle.get(handle)


def snap(*texts: str) -> dict[str, Any]:
    return {**TraceBuffer("codex").snapshot(), "items": [message(t) for t in texts], "events": 1}


def coordinator_double() -> tuple[SimpleNamespace, list[tuple[str, dict[str, Any]]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    return SimpleNamespace(trace_sink=lambda run_id, value: calls.append((run_id, value))), calls


def test_the_worker_hands_every_turn_of_a_capturing_dispatch_to_the_sink() -> None:
    me, calls = coordinator_double()
    port = TracePort({"d1": snap("first"), "d1-f1": snap("follow-up")})
    WorkCoordinator._admit_trace(  # type: ignore[arg-type]
        me,
        {"run_id": "run-1"},
        port,
        ["d1", "d1-f1"],
        DispatchOptions("m", None, capture_trace=True),
    )
    ((run_id, value),) = calls
    assert run_id == "run-1" and value["driver_id"] == "codex-cli"
    assert [t["turn"] for t in value["turns"]] == [0, 1]
    assert [t["items"][0]["text"] for t in value["turns"]] == ["first", "follow-up"]
    traces.validate_sanitized(value)


@pytest.mark.parametrize(
    "options", [None, DispatchOptions("m", None), DispatchOptions("m", None, capture_trace=False)]
)
def test_the_worker_never_calls_the_sink_without_capture_trace(options: Any) -> None:
    me, calls = coordinator_double()
    WorkCoordinator._admit_trace(  # type: ignore[arg-type]
        me, {"run_id": "run-1"}, TracePort({"d1": snap("x")}), ["d1"], options
    )
    assert calls == []


def test_the_worker_without_a_sink_captures_nothing() -> None:
    me = SimpleNamespace(trace_sink=None)
    WorkCoordinator._admit_trace(  # type: ignore[arg-type]
        me,
        {"run_id": "r"},
        TracePort({"d1": snap("x")}),
        ["d1"],
        DispatchOptions("m", None, capture_trace=True),
    )


def test_a_port_that_captures_nothing_is_skipped_and_a_failing_sink_is_ignored() -> None:
    me, calls = coordinator_double()
    options = DispatchOptions("m", None, capture_trace=True)
    WorkCoordinator._admit_trace(me, {"run_id": "r"}, object(), ["d1"], options)  # type: ignore[arg-type]
    WorkCoordinator._admit_trace(  # type: ignore[arg-type]
        me, {"run_id": "r"}, TracePort({"d1": None}), ["d1"], options
    )
    assert calls == []

    def boom(run_id: str, value: dict[str, Any]) -> None:
        raise RuntimeError("sink down")

    failing = SimpleNamespace(trace_sink=boom)
    WorkCoordinator._admit_trace(  # type: ignore[arg-type]
        failing, {"run_id": "r"}, TracePort({"d1": snap("x")}), ["d1"], options
    )  # trace capture never changes the run


def test_port_trace_reads_the_port_then_the_wrapped_driver() -> None:
    assert port_trace(object(), "h") is None
    assert port_trace(TracePort({"h": {"a": 1}}), "h") == {"a": 1}
    assert port_trace(TracePort({"h": "not a dict"}), "h") is None
    wrapper = SimpleNamespace(driver=TracePort({"h": {"b": 2}}))
    assert port_trace(wrapper, "h") == {"b": 2}


class NoOptions:
    driver_id = "opencode-server"  # a port without accepts_options (OpenCode, RecipePort, CliPort)


def check_options(port: Any, options: DispatchOptions | None) -> DispatchOptions | None:
    me = SimpleNamespace(store=None)  # the binding check is never reached for such a port
    dispatch = {"dispatch_id": "d1", "profile": {}}
    return WorkCoordinator._check_options(me, None, dispatch, port, options)  # type: ignore[arg-type]


def test_a_port_without_options_runs_a_capture_only_dispatch_uncaptured() -> None:
    # §9.1: OpenCode capture is deferred, not OpenCode trials: the trace flag alone reaches such
    # a port as no options instead of holding DRIVER_OPTIONS_UNSUPPORTED
    assert check_options(NoOptions(), DispatchOptions("m", None, capture_trace=True)) is None
    assert check_options(NoOptions(), DispatchOptions("m", None)) is None
    with pytest.raises(Hold) as held:  # any other option still holds, with or without capture
        check_options(NoOptions(), DispatchOptions("m", "high", capture_trace=True))
    assert held.value.code == "DRIVER_OPTIONS_UNSUPPORTED"


def resume_double(port: Any) -> SimpleNamespace:
    """A coordinator whose one execution of run-1 stored no options (it ran on ``port``) and is
    not resumed yet: the binding check passes or holds, then RESUME_NOT_COMMITTED follows."""
    data = {"dispatch": {"profile": {"driver_profile_ref": {"id": "p"}}, "node": {"strategy": "s"}}}
    me = SimpleNamespace(
        _limit=lambda seconds: 10.0,
        _run_execution=lambda worker, run_id: ("d1", {"state": "observing", "data": data}),
        registry=SimpleNamespace(resolve=lambda scope, ref, strategy: port),
        store=SimpleNamespace(head=lambda scope, kind, object_id: {"state": "running", "data": {}}),
    )
    me._takes_no_options = lambda scope, d: WorkCoordinator._takes_no_options(me, scope, d)  # type: ignore[arg-type]
    return me


def resume_code(port: Any, options: DispatchOptions) -> str:
    worker = SimpleNamespace(scope=None)
    with pytest.raises(Hold) as held:
        WorkCoordinator.continue_resumed(resume_double(port), worker, "run-1", options=options)  # type: ignore[arg-type]
    return str(held.value.code)


def test_a_resumed_turn_of_an_uncaptured_dispatch_keeps_its_binding() -> None:
    capture = DispatchOptions("m", None, capture_trace=True)
    # the port took no options: the capture-only options of the loop are the binding it ran with
    assert resume_code(NoOptions(), capture) == "RESUME_NOT_COMMITTED"
    # a port that takes options and stored none, or options beyond the flag, still differ
    accepts = SimpleNamespace(accepts_options=True)
    assert resume_code(accepts, capture) == "DISPATCH_OPTIONS_BINDING"
    assert resume_code(NoOptions(), DispatchOptions("m", "high")) == "DISPATCH_OPTIONS_BINDING"


def test_a_trace_sink_must_be_callable() -> None:
    with pytest.raises(RuntimeFault) as caught:
        WorkCoordinator(  # type: ignore[arg-type]
            SimpleNamespace(store=None), None, None, trace_sink="not callable"
        )
    assert caught.value.code == "WORKER_TRACE_SINK"


# ==================================================================================================
# read-only turns (§9.1): the same sanitizer, only when asked
# ==================================================================================================
EVENTS = [
    {"type": "thread.started", "thread_id": "t"},
    {"type": "item.completed", "item": {"type": "reasoning", "text": MARK}},
    {"type": "item.completed", "item": {"type": "agent_message", "text": '{"ok": true}'}},
]


# the turn's own sandbox credential literals (``readonly_turn._trace``): none of them is in EVENTS
TURN_CREDENTIALS = {"turn-credential-literal-not-in-the-events"}


def test_a_read_only_turn_returns_no_trace_unless_asked() -> None:
    assert readonly_turn._trace("codex", EVENTS, False, TURN_CREDENTIALS) is None


def test_a_read_only_turn_trace_is_the_sanitized_stream() -> None:
    value = readonly_turn._trace("codex", EVENTS, True, TURN_CREDENTIALS)
    assert value is not None and value["sanitizer_version"] == SANITIZER_VERSION
    assert [i["text"] for i in value["items"]] == ['{"ok": true}']
    assert value["dropped_event_types"] == {"thread.started": 1, "item.completed/reasoning": 1}
    assert MARK not in json.dumps(value)
    claude = readonly_turn._trace(
        "claude",
        [{"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": MARK}]}}],
        True,
        TURN_CREDENTIALS,
    )
    assert claude is not None and claude["items"] == [] and MARK not in json.dumps(claude)


def test_a_planner_reviewer_investigator_turn_is_a_named_turn_of_the_same_trace(w: World) -> None:
    run = combine(
        "codex-cli",
        [
            (0, snap("executor")),
            ("planner", snap("plan")),
            ("reviewer", snap("review")),
            ("investigator-1", snap("look")),
        ],
    )
    trial_head(w, "trial-named")
    ref = service(w).admit(
        run_id="run-named", goal_id="g", trial=context(trial_id="trial-named"),
        driver_id="codex-cli", sanitized=run,
    )  # fmt: skip
    body = service(w).read(proposer(w), ref)["body"]
    assert [t["turn"] for t in body["turns"]] == [0, "planner", "reviewer", "investigator-1"]


def test_read_only_turns_without_a_run_are_stored_under_the_goal_carrier(w: World) -> None:
    """§9.1: a planner turn runs before any run exists, so ``admit_turns`` stores it as the named
    turns of ``trace-<goal_id>.<turn>``, admitted like a run's trace (trial check, ACL)."""
    trial_head(w, "trial-turns")
    kwargs: dict[str, Any] = {
        "goal_id": "goal-t", "trial": context(trial_id="trial-turns"), "driver_id": "codex-cli",
        "turn": "planner", "snapshots": [snap("plan one"), snap("plan two")],
    }  # fmt: skip
    ref = service(w).admit_turns(**kwargs)
    assert ref is not None and ref["id"] == "trace-goal-t.planner"
    assert service(w).of_run("goal-t.planner") == ref
    read = service(w).read(proposer(w), ref)
    assert read["record"]["goal_id"] == "goal-t" and read["record"]["run_id"] == "goal-t.planner"
    assert [t["turn"] for t in read["body"]["turns"]] == ["planner", "planner"]
    assert service(w).admit_turns(**kwargs) == ref  # idempotent per goal and turn kind
    # a real goal (no trial context) stores nothing: trace-drop not_trial
    assert service(w).admit_turns(**{**kwargs, "goal_id": "goal-real", "trial": None}) is None
    assert drop_of(w, "goal-real.planner")["reason"] == "not_trial"
    assert service(w).of_run("goal-real.planner") is None
    # a validation trial's planner turn is stored, but never for the proposer
    trial_head(w, "trial-turns-v")
    other = service(w).admit_turns(
        **{**kwargs, "goal_id": "goal-v", "trial": context(trial_id="trial-turns-v",
                                                            split="validation")}
    )  # fmt: skip
    assert other is not None
    hold("TRACE_ACL", service(w).read, proposer(w), other)


@pytest.mark.parametrize("turn", ["executor", "investigator-x", "planner ", 0, None])
def test_admit_turns_names_only_read_only_turns(w: World, turn: Any) -> None:
    fault(
        "TRACE_TURN", service(w).admit_turns, goal_id="g", trial=None, driver_id="codex-cli",
        turn=turn, snapshots=[snap("x")],
    )  # fmt: skip
    assert service(w).records() == []


def test_of_run_is_none_for_a_run_without_a_stored_trace(w: World) -> None:
    assert service(w).of_run("run-never") is None
    trial_head(w, "trial-run-secret")
    dropped = service(w).admit(
        run_id="run-secret",
        goal_id="g",
        trial=context(trial_id="trial-run-secret"),
        driver_id="codex-cli",
        sanitized=sanitized(SECRET),
    )
    assert dropped is None
    assert service(w).of_run("run-secret") is None  # a trace-drop is not a trace


# ==================================================================================================
# end to end: trial goals capture, real goals never do (D-100, AC-07 unit part)
# ==================================================================================================
TRACED_AGENT = (
    AGENT
    + r"""
sys.stdout.write(json.dumps({"type": "item.completed", "item": {"type": "agent_message",
                             "text": "patched value() to return 2"}}) + "\n")
sys.stdout.write(json.dumps({"type": "item.completed", "item": {"type": "reasoning",
                             "text": "PRIVATE-REASONING"}}) + "\n")
sys.stdout.write(json.dumps({"type": "item.completed", "item": {"type": "command_execution",
                             "command": "cat secrets", "aggregated_output": "OUTPUT-BODY"}}) + "\n")
"""
)


@pytest.fixture
def setup(deployment: Any, tmp_path: Path) -> Setup:
    built = Setup(deployment, tmp_path)
    original = built.container.command

    def traced(argv: list[str], workspace: Path, run_name: str, **kw: Any) -> list[str]:
        command: list[str] = original(argv, workspace, run_name, **kw)
        command[2] = TRACED_AGENT  # the scripted agent also prints provider items
        return command

    built.container.command = traced  # type: ignore[method-assign]
    return built


def trial_context_for(capture: bool) -> TrialContext:
    return TrialContext(
        subject={"experiment_id": "exp-1", "trial_id": "trial-1"}, arm="candidate",
        cell_id=HIGH_ID, split="development", capture_trace=capture, planner_mode="fixed",
        environment_id="app",
    )  # fmt: skip


def run_trial(setup: Setup, text: str, capture: bool) -> str:
    rig = setup.rig
    composition = rig.service.apps["app"].compositions[HIGH_ID]
    goal = submit(rig, text)
    rig.service.plan(goal, composition=composition, trial=trial_context_for(capture))
    rig.service.approve(rig.operator, goal)
    trials = ExecutionLoop(rig.service, setup.coordinator, publisher=None)
    assert trials.run_goal(goal)["status"] == "verified"
    return goal


def test_a_capturing_trial_goal_reaches_the_sink_sanitized(setup: Setup) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    setup.coordinator.trace_sink = lambda run_id, value: calls.append((run_id, value))
    run_trial(setup, "traced trial: make value return 2", capture=True)
    ((_run_id, value),) = calls
    traces.validate_sanitized(value)
    items = [i for t in value["turns"] for i in t["items"]]
    assert [i["text"] for i in items] == ["patched value() to return 2"]
    flat = json.dumps(value)
    assert (
        "PRIVATE-REASONING" not in flat and "OUTPUT-BODY" not in flat and "cat secrets" not in flat
    )
    assert value["dropped_event_types"].get("item.completed/reasoning") == 1
    assert value["dropped_event_types"].get("item.completed/command_execution") == 1
    assert value["driver_id"]


def test_a_trial_goal_without_capture_and_a_real_goal_never_reach_the_sink(setup: Setup) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    setup.coordinator.trace_sink = lambda run_id, value: calls.append((run_id, value))
    run_trial(setup, "untraced trial: make value return 2", capture=False)
    real = setup.goal(setup.rig.service.apps["app"].compositions[HIGH_ID])
    assert setup.loop.run_goal(real)["status"] == "published"
    assert calls == []  # a real goal never captures (D-100)
    # and the driver kept no buffer: its trace is empty for both
    assert setup.container.driver._traces == {}


def test_a_capturing_trial_on_a_port_without_options_runs_uncaptured(
    deployment: Any, tmp_path: Path
) -> None:
    # a cell whose port takes no dispatch options (OpenCode-like): the trial completes, nothing
    # is held and the sink is never called (OpenCode capture is deferred, §9.1)
    built = Setup(deployment, tmp_path, plain_port=True)
    calls: list[tuple[str, dict[str, Any]]] = []
    built.coordinator.trace_sink = lambda run_id, value: calls.append((run_id, value))
    rig = built.rig
    goal = submit(rig, "plain port trial: make value return 2")
    rig.service.plan(
        goal, composition=rig.service.apps["app"].compositions[CELL],
        trial=replace(trial_context_for(True), cell_id=CELL),
    )  # fmt: skip
    rig.service.approve(rig.operator, goal)
    record = ExecutionLoop(rig.service, built.coordinator, publisher=None).run_goal(goal)
    assert record["status"] == "verified", record
    assert calls == [] and built.container.driver._traces == {}
    assert len(built.container.argvs) == 1  # it ran, with today's argv
    argv = built.container.argvs[0]  # no effort flag; decision (C)'s web search override only
    assert argv.count("-c") == 1 and argv[argv.index("-c") + 1] == 'web_search="disabled"'


def test_the_deployment_sink_stores_the_trial_trace_and_nothing_for_a_real_goal(
    setup: Setup,
) -> None:
    d = setup.d
    service_ = setup.rig.service
    with d.store.tx() as db:
        d.store.cas(db, d.scope, "eval-trial", "trial-1", 0, "running", {"task_id": "bug-dev-00"})
    setup.coordinator.trace_sink = trace_sink(
        d.store, d.scope, d.artifacts, lambda goal_id: service_.plan_record(goal_id)
    )
    goal = run_trial(setup, "stored trial: make value return 2", capture=True)
    real = setup.goal(service_.apps["app"].compositions[HIGH_ID])
    assert setup.loop.run_goal(real)["status"] == "published"
    store_traces = TraceService(d.store, d.scope, d.artifacts)
    records = [v for _r, v in store_traces.records()]
    assert len(records) == 1  # the real goal left no trace and no drop
    record = records[0]
    assert record["goal_id"] == goal and record["split"] == "development"
    assert record["task_id"] == "bug-dev-00" and record["cell_id"] == HIGH_ID
    assert store_traces.counts() == {"traces": {"development": 1}, "drops": {}}
    body = json.loads(d.artifacts.read(d.scope, record["artifact"]))
    assert "patched value() to return 2" in json.dumps(body)
    assert "PRIVATE-REASONING" not in json.dumps(body)
    real_runs = [v for _r, v in store_traces.records() if v["goal_id"] == real]
    assert real_runs == []


# ==================================================================================================
# IC-28 (provisional): an executor turn's own credential literals drop its trace (cli.py)
# ==================================================================================================
LEASED = "sk-leased-Zq9wXk3LmN8vBc2RtY6uHj4PaaaaBBBB"  # outside the §9.2 patterns (as auth.json)
REFRESHED = "rt-refreshed-Qw8eRt7yUi6oPa5sDf4gHj3kZZZ"


def credential_setup(setup: Setup, tmp_path: Path, tail: str) -> Setup:
    """The scoped Codex credential the port leases holds ``LEASED``; the scripted agent prints
    the ``TRACED_AGENT`` items and then runs ``tail``."""
    (tmp_path / "scoped-codex" / ".codex" / "auth.json").write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": LEASED}})
    )
    original = setup.container.command

    def echoing(argv: list[str], workspace: Path, run_name: str, **kw: Any) -> list[str]:
        command: list[str] = original(argv, workspace, run_name, **kw)
        command[2] = command[2] + tail
        return command

    setup.container.command = echoing  # type: ignore[method-assign]
    return setup


def stored_by_sink(setup: Setup) -> TraceService:
    d = setup.d
    with d.store.tx() as db:
        d.store.cas(db, d.scope, "eval-trial", "trial-1", 0, "running", {"task_id": "bug-dev-00"})
    setup.coordinator.trace_sink = trace_sink(
        d.store, d.scope, d.artifacts, lambda goal_id: setup.rig.service.plan_record(goal_id)
    )
    return TraceService(d.store, d.scope, d.artifacts)


def message_line(text: str) -> str:
    """Script lines that print one Codex agent message holding ``text``."""
    event = {"type": "item.completed", "item": {"type": "agent_message", "text": text}}
    return f"\nsys.stdout.write({json.dumps(json.dumps(event))} + chr(10))\n"


ECHO_LEASED = message_line("the token is " + LEASED)
# the run rotates its credential (a refresh) and a kept message quotes the new one: the kept text
# is checked against the credential as the run left it
ECHO_REFRESHED = (
    "\n(home / '.codex' / 'auth.json').write_text("
    f"json.dumps({{'tokens': {{'refresh_token': {REFRESHED!r}}}}}))\n"
) + message_line("refreshed to " + REFRESHED)


@pytest.mark.parametrize("tail", [ECHO_LEASED, ECHO_REFRESHED], ids=["leased", "refreshed"])
def test_an_executor_turn_that_echoes_its_own_credential_stores_no_trace(
    setup: Setup, tmp_path: Path, tail: str
) -> None:
    service_ = stored_by_sink(credential_setup(setup, tmp_path, tail))
    goal = run_trial(setup, "echoing trial: make value return 2", capture=True)  # verified
    assert service_.records() == []  # trace capture never changes the run
    assert service_.counts() == {"traces": {}, "drops": {"sanitizer_error": 1}}
    d = setup.d
    ((_ref, drop),) = list(d.store.list_objects(d.scope, DROP_KIND))
    assert drop["reason"] == "sanitizer_error"
    for kind in (DROP_KIND, TRACE_KIND, "execution-plan"):
        for _r, value in d.store.list_objects(d.scope, kind):
            assert LEASED not in json.dumps(value) and REFRESHED not in json.dumps(value)
    assert setup.rig.service.plan_record(goal)["trial"]["capture_trace"] is True


def test_an_executor_turn_without_its_credential_in_the_events_is_stored(
    setup: Setup, tmp_path: Path
) -> None:
    service_ = stored_by_sink(credential_setup(setup, tmp_path, ""))
    run_trial(setup, "clean trial: make value return 2", capture=True)
    assert service_.counts() == {"traces": {"development": 1}, "drops": {}}


def test_the_driver_forgets_the_dispatch_credential_literals_on_destroy(
    setup: Setup, tmp_path: Path
) -> None:
    driver = credential_setup(setup, tmp_path, ECHO_LEASED).container.driver
    run_trial(setup, "forgetting trial: make value return 2", capture=True)
    # every dispatch was destroyed after its run: nothing of its credential stays in memory
    assert driver._credentials == {} and driver._credential_homes == {}
    assert driver._credential_hits == set() and driver._traces == {}
