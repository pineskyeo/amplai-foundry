"""DEV-03 read-only metrics and bounded durable telemetry acceptance."""

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from amplai_foundry.control_plane.api_v3.server import ApiServices, create_app
from amplai_foundry.evaluation.observatory import Observatory
from amplai_foundry.evaluation.telemetry import TelemetrySpool
from amplai_foundry.runtime.errors import RuntimeFault
from amplai_foundry.runtime.storage.store import Scope, Store


def change_run(deployment, run_id, **updates):
    # Controlled corruption/usage fixture; never a public write path.
    s, scope = deployment.store, deployment.scope
    with s.tx() as db:
        h = s.head(scope, "run", run_id, db=db)
        s.cas(
            db,
            scope,
            "run",
            run_id,
            h["row_version"],
            h["state"],
            {**h["data"], "record": {**h["data"]["record"], **updates}},
        )


def test_dev03_empty_metrics_are_unknown_not_zero_success(deployment):
    value = Observatory(deployment.store).summary(deployment.scope)
    assert value["verified_goal_rate"] is None
    assert value["first_attempt_verified_rate"] is None
    assert value["total_cost_microunits"] is None
    assert value["acceptance"]["coverage"] is None
    assert value["run_count"] == value["goal_count"] == 0


def test_dev03_real_goal_denominator_and_coverage(deployment, prepared):
    result = deployment.execute(prepared)
    v = Observatory(deployment.store).summary(deployment.scope)
    assert v["run_count"] == 2 and v["goal_count"] == 1
    assert v["verified_goals"] == 1 and v["verified_goal_rate"] == 1
    assert v["first_attempt_verified_goals"] == 1
    assert v["acceptance"]["mandatory"] == v["acceptance"]["with_pass"] == 2
    assert v["acceptance"]["coverage"] == 1
    assert v["total_cost_microunits"] == 0
    assert v["run_wall_elapsed_ms"]["samples"] == 2
    assert v["compute_ms"] is None and v["queue_ms"] is None
    assert v["integrity_findings"] == []
    assert v["slices"]["task_class"] == {"unreported": 2}
    assert result["status"] == "verified"


@pytest.mark.parametrize("status,cost", [("unknown", None), ("estimated", 70), ("measured", None)])
def test_dev03_unknown_or_estimated_cost_never_becomes_actual_total(
    deployment, prepared, status, cost
):
    r = deployment.execute(prepared)["runs"][0]["run_id"]
    usage = {
        "input_tokens": 3,
        "output_tokens": 7,
        "cost_microunits": cost,
        "currency": "USD",
        "status": status,
        "source_ref": None,
    }
    change_run(deployment, r, usage=usage)
    v = Observatory(deployment.store).summary(deployment.scope)
    assert v["total_cost_microunits"] is None
    assert v["cost_by_currency"]["USD"]["total_cost_microunits"] is None
    assert v["unknown_cost_run_count"] == (0 if status == "estimated" else 1)


def test_dev03_distinct_currencies_not_summed(deployment, prepared):
    result = deployment.execute(prepared)
    for run, currency in zip(result["runs"], ["USD", "KRW"], strict=False):
        change_run(
            deployment,
            run["run_id"],
            usage={
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_microunits": 5,
                "currency": currency,
                "status": "measured",
                "source_ref": None,
            },
        )
    v = Observatory(deployment.store).summary(deployment.scope)
    assert v["total_cost_microunits"] is None and v["known_cost_sum_microunits"] is None
    assert v["cost_by_currency"]["KRW"]["measured_microunits"] == 5


@pytest.mark.parametrize("dim", ["composition", "model", "driver", "risk", "repo", "task_class"])
def test_dev03_scoped_metric_filters(deployment, prepared, dim):
    deployment.execute(prepared)
    o = Observatory(deployment.store)
    v = o.summary(deployment.scope)
    chosen = next(iter(v["slices"][dim]))
    filtered = o.summary(deployment.scope, filters={dim: chosen})
    assert filtered["run_count"] == v["slices"][dim][chosen]
    assert filtered["goal_count"] == 1
    assert o.summary(deployment.scope, filters={dim: "not-present"})["goal_count"] == 0
    assert o.summary(Scope("foreign", "project"))["run_count"] == 0


@pytest.mark.parametrize(
    "kwargs,expected_code",
    [
        ({"since": "2026-09-16"}, "METRIC_TIME"),
        ({"until": "bad"}, "METRIC_TIME"),
        ({"since": 1}, "METRIC_TIME"),
        (
            {"since": "2026-09-16T01:00:00Z", "until": "2026-09-16T00:00:00Z"},
            "METRIC_WINDOW",
        ),
        (
            {"since": "2026-09-16T00:00:00Z", "until": "2026-09-16T00:00:00Z"},
            "METRIC_WINDOW",
        ),
        ({"filters": {"unrecognized": "a"}}, "METRIC_FILTER"),
        ({"filters": {"model": ""}}, "METRIC_FILTER"),
    ],
)
def test_dev03_bad_metric_slice_rejected(deployment, kwargs, expected_code):
    with pytest.raises(RuntimeFault) as exc_info:
        Observatory(deployment.store).summary(deployment.scope, **kwargs)
    assert exc_info.value.code == expected_code


def test_dev03_window_is_half_open_run_start(deployment, prepared):
    runs = deployment.execute(prepared)["runs"]
    for i, r in enumerate(runs):
        change_run(
            deployment,
            r["run_id"],
            started_at=f"2026-09-16T0{i}:00:00Z",
            finished_at=f"2026-09-16T0{i}:01:00Z",
        )
    v = Observatory(deployment.store).summary(
        deployment.scope, since="2026-09-16T00:00:00Z", until="2026-09-16T01:00:00Z"
    )
    assert v["run_count"] == 1 and v["goal_count"] == 1
    assert v["run_wall_elapsed_ms"]["median"] == 60000


def test_dev03_false_goal_head_without_global_proof_is_not_success(deployment, prepared):
    s = deployment.store
    with s.tx() as db:
        h = s.head(deployment.scope, "goal", prepared["goal_id"], db=db)
        s.cas(
            db,
            deployment.scope,
            "goal",
            prepared["goal_id"],
            h["row_version"],
            "verified",
            h["data"],
        )
    v = Observatory(s).summary(deployment.scope)
    assert v["verified_goals"] == v["first_attempt_verified_goals"] == 0
    assert v["integrity_findings"][0]["finding"] == "missing_current_global_verification"


def test_dev03_negative_wall_duration_not_compute_time(deployment, prepared):
    run = deployment.execute(prepared)["runs"][0]["run_id"]
    change_run(
        deployment, run, started_at="2026-09-16T01:00:00Z", finished_at="2026-09-16T00:00:00Z"
    )
    v = Observatory(deployment.store).summary(deployment.scope)
    assert v["run_wall_elapsed_ms"]["samples"] == 1
    assert any(f.get("finding") == "negative_wall_duration" for f in v["integrity_findings"])


def emit(store, scope, n):
    with store.tx() as db:
        for _ in range(n):
            store.event(
                db,
                scope,
                "goal",
                "g-test",
                "goal.observed",
                {"prompt": "DO NOT EXPORT", "reasoning": "DO NOT EXPORT"},
            )


def test_dev03_telemetry_overflow_preserves_authority_log(tmp_path):
    scope = Scope("tenant", "project")
    with Store(tmp_path / "state") as store:
        emit(store, scope, 6)
        spool = TelemetrySpool(store, tmp_path / "telemetry.db", max_events=2, max_bytes=20000)
        status = spool.capture(scope)
        assert status["queued"] == 2 and status["dropped"] == 4
        assert len(store.events(scope)) == 6
        assert status["authority_events_deleted"] == 0
        batch = []
        spool.drain(scope, batch.extend)
        assert len(batch) == 2
        assert all(e["payload_exported"] is False for e in batch)
        assert "DO NOT EXPORT" not in json.dumps(batch)
        spool.close()
        spool = TelemetrySpool(store, tmp_path / "telemetry.db", max_events=2, max_bytes=20000)
        assert spool.capture(scope)["queued"] == 0
        assert spool.status(scope)["exported"] == 2
        spool.close()


def test_dev03_export_failure_retry_exact_ids_and_isolated_scopes(tmp_path):
    a, b = Scope("tenant", "a"), Scope("tenant", "b")
    with Store(tmp_path / "state") as store:
        emit(store, a, 2)
        emit(store, b, 1)
        spool = TelemetrySpool(store, tmp_path / "spool.db", max_events=10, max_bytes=10000)
        spool.capture(a)
        spool.capture(b)
        attempts = []

        def failed(batch):
            attempts.extend(batch)
            raise ConnectionError("collector unavailable")

        first = spool.drain(a, failed)
        assert first["delivery"] == "retry_pending" and first["queued"] == 2
        second = []
        spool.drain(a, second.extend)
        assert [r["event_id"] for r in attempts] == [r["event_id"] for r in second]
        assert spool.status(b)["queued"] == 1
        spool.close()


@pytest.mark.parametrize("events,size", [(0, 1024), (True, 1024), (2, 0), (2, True)])
def test_dev03_spool_requires_explicit_finite_bounds(tmp_path, events, size):
    with Store(tmp_path / "state") as store, pytest.raises(RuntimeFault):
        TelemetrySpool(store, tmp_path / "spool.db", max_events=events, max_bytes=size)


def test_dev03_telemetry_byte_limit_and_symlinks(tmp_path):
    scope = Scope("tenant", "project")
    with Store(tmp_path / "state") as store:
        emit(store, scope, 10)
        spool = TelemetrySpool(store, tmp_path / "spool.db", max_events=100, max_bytes=1024)
        v = spool.capture(scope)
        assert v["queued_bytes"] <= 1024 and v["dropped"] > 0
        spool.close()
        (tmp_path / "link.db").symlink_to(tmp_path / "spool.db")
        with pytest.raises(RuntimeFault):
            TelemetrySpool(store, tmp_path / "link.db", max_events=10, max_bytes=10000)


def test_dev03_observatory_http_uses_scoped_auth_and_safe_projection(deployment, prepared):
    deployment.execute(prepared)
    actor = replace(deployment.actor, permissions=frozenset({"runtime.read"}))
    services = ApiServices(deployment.runtime, deployment.goals, lambda authorization: actor)
    with TestClient(create_app(services)) as client:
        v = client.get("/api/v3/metrics", params={"repo": "demo:alpha"}).json()
        assert v["run_count"] == 1 and v["goal_count"] == 1
        v = client.get("/api/v3/telemetry/events").json()
        assert v["payload_exported"] is False and v["events"]
        assert "data" not in v["events"][0]
        assert client.get("/api/v3/metrics", params={"since": "bad"}).status_code >= 400
