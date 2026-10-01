"""Work 033 S12: quota observation without a quota API (interfaces.md §3.13, §8.6, §2.14).

`QuotaObserver` measures proxies from the store: runs and tokens per driver in rolling 1 h, 5 h,
24 h and 7 d windows, `record` writes one `quota-observation` per driver and night
(`quota-<driver>-<YYYY-MM-DD>`), and `headroom` gives the pilot's suggested B
(`plan.md` §10.5: B = floor(keep x headroom / median tokens per trial), keep 0.5). Rate-limit and
limit-error counts are not stored anywhere yet (§14 Q5), so they are 0 and `signal_source` says
so; nothing here guesses a field name. Real `Store` in `tmp_path`; no driver or network.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.meta_harness.quota import (
    SIGNAL_SOURCE,
    WINDOWS,
    QuotaObserver,
    QuotaWindow,
)
from amplai_foundry.runtime.errors import RuntimeFault
from amplai_foundry.runtime.storage.store import Scope, Store

SCOPE = Scope("tenant", "project")
NOW = datetime(2026, 10, 2, 3, 0, 0, tzinfo=UTC).timestamp()
HOUR = 3600


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class World:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.driver_refs: dict[str, dict[str, Any]] = {}
        self.runs = 0

    def put(self, kind: str, object_id: str, value: dict[str, Any], revision: int = 1) -> dict:
        with self.store.tx() as db:
            return self.store.put(
                db, SCOPE, kind, object_id, revision, {"scope": SCOPE.wire(), **value}
            )

    def driver(self, driver_id: str) -> dict[str, Any]:
        if driver_id not in self.driver_refs:
            self.driver_refs[driver_id] = self.put(
                "driver-capabilities", f"cap-{driver_id}",
                {"driver_id": driver_id, "driver_version": "1.0"},
            )  # fmt: skip
        return self.driver_refs[driver_id]

    def run(
        self,
        driver: str,
        *,
        ago: float,
        tokens_in: object = 100,
        tokens_out: object = 10,
        cached: int | None = None,
        cached_key: str = "cached_input_tokens",
        key: str | None = None,
        started: bool = False,
        revision: int = 1,
    ) -> str:
        self.runs += 1
        run_id = key or f"run-{self.runs}"
        usage: dict[str, Any] = {"input_tokens": tokens_in, "output_tokens": tokens_out}
        if cached is not None:
            detail = self.put("usage-detail", f"detail-{run_id}-{revision}", {cached_key: cached})
            usage["source_ref"] = detail
        at = iso(NOW - ago)
        value: dict[str, Any] = {
            "driver_profile_ref": self.driver(driver),
            "usage": usage,
            "started_at" if started else "finished_at": at,
        }
        self.put("run-record", run_id, value, revision)
        return run_id


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    with Store(tmp_path / "runtime", clock=lambda: NOW) as store:
        yield World(store)


def observer(world: World) -> QuotaObserver:
    return QuotaObserver(world.store, SCOPE)


def by_window(windows: list[QuotaWindow], driver: str) -> dict[str, QuotaWindow]:
    return {w.window: w for w in windows if w.driver_id == driver}


# --- observe -----------------------------------------------------------------------------------


def test_each_driver_gets_the_four_rolling_windows_in_order(world: World) -> None:
    world.run("codex-cli", ago=60)
    world.run("claude-cli", ago=60)
    windows = observer(world).observe(until=iso(NOW))
    assert [(w.driver_id, w.window) for w in windows] == [
        (d, name) for d in ("claude-cli", "codex-cli") for name in ("1h", "5h", "24h", "7d")
    ]
    assert tuple(WINDOWS) == ("1h", "5h", "24h", "7d")


def test_runs_and_tokens_fall_into_the_windows_they_finished_in(world: World) -> None:
    world.run("codex-cli", ago=0.5 * HOUR, tokens_in=100, tokens_out=10)
    world.run("codex-cli", ago=3 * HOUR, tokens_in=200, tokens_out=20)
    world.run("codex-cli", ago=12 * HOUR, tokens_in=400, tokens_out=40)
    world.run("codex-cli", ago=3 * 24 * HOUR, tokens_in=800, tokens_out=80)
    got = by_window(observer(world).observe(until=iso(NOW)), "codex-cli")
    assert (got["1h"].runs, got["1h"].input_tokens, got["1h"].output_tokens) == (1, 100, 10)
    assert (got["5h"].runs, got["5h"].input_tokens, got["5h"].output_tokens) == (2, 300, 30)
    assert (got["24h"].runs, got["24h"].input_tokens, got["24h"].output_tokens) == (3, 700, 70)
    assert (got["7d"].runs, got["7d"].input_tokens, got["7d"].output_tokens) == (4, 1500, 150)


def test_a_run_outside_seven_days_or_after_until_is_not_counted(world: World) -> None:
    world.run("codex-cli", ago=8 * 24 * HOUR)
    world.run("codex-cli", ago=-HOUR)  # finishes after `until`
    assert observer(world).observe(until=iso(NOW)) == []


def test_only_the_latest_revision_of_a_run_counts(world: World) -> None:
    world.run("codex-cli", ago=60, key="run-a", tokens_in=100, revision=1)
    world.run("codex-cli", ago=60, key="run-a", tokens_in=999, revision=2)
    got = by_window(observer(world).observe(until=iso(NOW)), "codex-cli")
    assert (got["1h"].runs, got["1h"].input_tokens) == (1, 999)


def test_a_running_run_counts_from_its_start_time(world: World) -> None:
    world.run("codex-cli", ago=2 * HOUR, started=True)
    got = by_window(observer(world).observe(until=iso(NOW)), "codex-cli")
    assert (got["1h"].runs, got["5h"].runs) == (0, 1)


def test_unknown_token_counts_are_zero_but_the_run_is_still_counted(world: World) -> None:
    world.run("codex-cli", ago=60, tokens_in=None, tokens_out="many")
    world.run("codex-cli", ago=60, tokens_in=-5, tokens_out=True)
    got = by_window(observer(world).observe(until=iso(NOW)), "codex-cli")
    assert (got["1h"].runs, got["1h"].input_tokens, got["1h"].output_tokens) == (2, 0, 0)


def test_cached_input_tokens_come_from_the_usage_detail_of_either_provider(world: World) -> None:
    world.run("codex-cli", ago=60, cached=30)
    world.run("codex-cli", ago=60, cached=12, cached_key="cache_read_input_tokens")
    world.run("codex-cli", ago=60)  # no usage-detail
    got = by_window(observer(world).observe(until=iso(NOW)), "codex-cli")
    assert got["1h"].cached_input_tokens == 42


def test_a_run_of_an_unresolvable_driver_profile_is_left_out(world: World) -> None:
    world.runs += 1
    world.put("run-record", "run-orphan", {
        "driver_profile_ref": {"id": "ghost", "revision": 1, "digest": "sha256:" + "0" * 64},
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "finished_at": iso(NOW - 60),
    })  # fmt: skip
    assert observer(world).observe(until=iso(NOW)) == []


def test_rate_limit_counts_are_zero_and_never_guessed(world: World) -> None:
    world.run("codex-cli", ago=60)
    for window in observer(world).observe(until=iso(NOW)):
        assert (window.rate_limit_events, window.limit_errors, window.first_signal_at) == (
            0, 0, None,
        )  # fmt: skip


@pytest.mark.parametrize("until", ["", "not-a-time", "2026-10-02T03:00:00", "2026-10-02", 5])
def test_until_must_be_an_iso_utc_time(world: World, until: Any) -> None:
    with pytest.raises(RuntimeFault) as fault:
        observer(world).observe(until=until)
    assert fault.value.code == "QUOTA_UNTIL" and fault.value.outcome == "rejected"


def test_observe_writes_nothing(world: World) -> None:
    world.run("codex-cli", ago=60)
    before = world.store.conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
    observer(world).observe(until=iso(NOW))
    assert world.store.conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0] == before


# --- record ------------------------------------------------------------------------------------


def test_record_writes_one_observation_per_driver_and_night(world: World) -> None:
    world.run("codex-cli", ago=60)
    world.run("claude-cli", ago=60)
    refs = observer(world).record("2026-10-01")
    assert sorted(r["id"] for r in refs) == [
        "quota-claude-cli-2026-10-01", "quota-codex-cli-2026-10-01",
    ]  # fmt: skip
    value = world.store.get(SCOPE, "quota-observation", refs[1])
    assert value["driver_id"] == "codex-cli" and value["night"] == "2026-10-01"
    assert value["until"] == iso(NOW)
    assert [w["window"] for w in value["windows"]] == ["1h", "5h", "24h", "7d"]
    assert value["signal_source"] == SIGNAL_SOURCE == "none_stored_v1"
    assert value["windows"][0]["runs"] == 1


def test_recording_the_same_night_again_is_a_new_revision(world: World) -> None:
    world.run("codex-cli", ago=60)
    first = observer(world).record("2026-10-01")
    second = observer(world).record("2026-10-01")
    assert (first[0]["revision"], second[0]["revision"]) == (1, 2)
    assert first[0]["id"] == second[0]["id"]


def test_record_without_any_run_writes_nothing(world: World) -> None:
    assert observer(world).record("2026-10-01") == []


@pytest.mark.parametrize("night", ["2026-1-1", "tonight", "", "2026-10-01T01:00", None])
def test_a_night_must_be_a_date(world: World, night: Any) -> None:
    with pytest.raises(RuntimeFault) as fault:
        observer(world).record(night)
    assert fault.value.code == "QUOTA_NIGHT"


def test_latest_is_the_newest_observation_or_none(world: World) -> None:
    assert observer(world).latest() is None
    world.run("codex-cli", ago=60)
    observer(world).record("2026-10-01")
    latest = observer(world).latest()
    assert latest is not None and latest["id"] == "quota-codex-cli-2026-10-01"


# --- headroom (the pilot, plan.md §10.5) -------------------------------------------------------

PILOT = ["2026-10-01", "2026-10-02", "2026-10-03"]


def observation(
    world: World, driver: str, night: str, *, tokens_5h: tuple[int, int], until: float,
    first_signal_at: str | None = None,
) -> None:  # fmt: skip
    windows = []
    for name in WINDOWS:
        windows.append({
            "driver_id": driver, "window": name, "runs": 1,
            "input_tokens": tokens_5h[0], "output_tokens": tokens_5h[1],
            "cached_input_tokens": 0, "rate_limit_events": 0, "limit_errors": 0,
            "first_signal_at": first_signal_at if name == "5h" else None,
        })  # fmt: skip
    world.put("quota-observation", f"quota-{driver}-{night}", {
        "driver_id": driver, "night": night, "until": iso(until), "windows": windows,
        "signal_source": SIGNAL_SOURCE,
    })  # fmt: skip


def trial(world: World, driver: str, n: int, *, tokens: tuple[Any, Any], at: float) -> None:
    driver_ref = world.driver(driver)
    composition = world.put(
        "harness-composition", f"comp-{driver}", {"driver_profile_ref": driver_ref}
    )
    world.put("eval-trial", f"trial-{driver}-{n}", {
        "composition_ref": composition, "finished_at": iso(at),
        "input_tokens": tokens[0], "output_tokens": tokens[1],
    })  # fmt: skip


def test_headroom_is_the_largest_5h_window_when_no_limit_signal_was_seen(world: World) -> None:
    for i, night in enumerate(PILOT):
        observation(world, "codex-cli", night, tokens_5h=(1000 * (i + 1), 0), until=NOW - i * 10)
    for n in range(4):
        trial(world, "codex-cli", n, tokens=(60, 40), at=NOW - 60)  # 100 tokens per trial
    result = observer(world).headroom(PILOT)
    driver = result["drivers"]["codex-cli"]
    assert driver["basis"] == "at_least_max_observed"
    assert driver["headroom_tokens"] == 3000
    assert driver["median_tokens_per_trial"] == 100 and driver["trials_measured"] == 4
    assert driver["suggested_budget_trials"] == 15  # floor(0.5 x 3000 / 100)
    assert result["suggested_budget_trials"] == 15
    assert result["signal_source"] == SIGNAL_SOURCE and result["keep_operator_share"] == 0.5


def test_headroom_at_the_first_limit_signal_takes_that_window(world: World) -> None:
    observation(world, "codex-cli", PILOT[0], tokens_5h=(9000, 0), until=NOW - 100,
                first_signal_at=iso(NOW - 500))  # fmt: skip
    observation(world, "codex-cli", PILOT[1], tokens_5h=(4000, 1000), until=NOW,
                first_signal_at=iso(NOW - 900))  # fmt: skip
    trial(world, "codex-cli", 0, tokens=(50, 50), at=NOW - 60)
    driver = observer(world).headroom(PILOT)["drivers"]["codex-cli"]
    assert driver["basis"] == "first_signal" and driver["headroom_tokens"] == 5000
    assert driver["suggested_budget_trials"] == 25


def test_keep_scales_the_suggestion_and_the_median_is_a_median(world: World) -> None:
    observation(world, "codex-cli", PILOT[0], tokens_5h=(10000, 0), until=NOW)
    for n, size in enumerate((10, 20, 1000)):  # median 20, not the mean
        trial(world, "codex-cli", n, tokens=(size, 0), at=NOW - 60)
    result = observer(world).headroom(PILOT, keep=0.25)
    assert result["drivers"]["codex-cli"]["median_tokens_per_trial"] == 20
    assert result["drivers"]["codex-cli"]["suggested_budget_trials"] == 125
    assert result["keep_operator_share"] == 0.25


def test_without_a_measured_trial_there_is_no_suggestion(world: World) -> None:
    observation(world, "codex-cli", PILOT[0], tokens_5h=(10000, 0), until=NOW)
    result = observer(world).headroom(PILOT)
    assert result["drivers"]["codex-cli"]["suggested_budget_trials"] is None
    assert result["drivers"]["codex-cli"]["median_tokens_per_trial"] is None
    assert result["suggested_budget_trials"] is None


def test_trials_with_an_unknown_token_count_or_outside_the_night_are_not_measured(
    world: World,
) -> None:
    observation(world, "codex-cli", PILOT[0], tokens_5h=(10000, 0), until=NOW)
    trial(world, "codex-cli", 0, tokens=(None, 10), at=NOW - 60)
    trial(world, "codex-cli", 1, tokens=(10, None), at=NOW - 60)
    trial(world, "codex-cli", 2, tokens=(10, 10), at=NOW - 3 * 24 * HOUR)
    trial(world, "codex-cli", 3, tokens=(40, 10), at=NOW - 60)
    driver = observer(world).headroom(PILOT)["drivers"]["codex-cli"]
    assert driver["trials_measured"] == 1 and driver["median_tokens_per_trial"] == 50


def test_the_binding_driver_is_the_smallest_suggestion(world: World) -> None:
    observation(world, "codex-cli", PILOT[0], tokens_5h=(10000, 0), until=NOW)
    observation(world, "claude-cli", PILOT[0], tokens_5h=(2000, 0), until=NOW)
    trial(world, "codex-cli", 0, tokens=(100, 0), at=NOW - 60)
    trial(world, "claude-cli", 0, tokens=(100, 0), at=NOW - 60)
    result = observer(world).headroom(PILOT)
    assert result["drivers"]["codex-cli"]["suggested_budget_trials"] == 50
    assert result["drivers"]["claude-cli"]["suggested_budget_trials"] == 10
    assert result["suggested_budget_trials"] == 10


def test_nights_outside_the_pilot_do_not_count(world: World) -> None:
    observation(world, "codex-cli", "2026-09-01", tokens_5h=(99999, 0), until=NOW)
    observation(world, "codex-cli", PILOT[0], tokens_5h=(100, 0), until=NOW)
    assert observer(world).headroom(PILOT)["drivers"]["codex-cli"]["headroom_tokens"] == 100


def test_headroom_never_writes_or_changes_a_config(world: World) -> None:
    observation(world, "codex-cli", PILOT[0], tokens_5h=(100, 0), until=NOW)
    before = world.store.conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
    observer(world).headroom(PILOT)
    assert world.store.conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0] == before


@pytest.mark.parametrize("nights", ["2026-10-01", ["tonight"], [1], ["2026-1-1"]])
def test_headroom_pilot_nights_are_dates(world: World, nights: Any) -> None:
    with pytest.raises(RuntimeFault) as fault:
        observer(world).headroom(nights)
    assert fault.value.code == "QUOTA_NIGHT"


@pytest.mark.parametrize("keep", [-0.1, 1.5, "half", None])
def test_keep_is_a_share_between_zero_and_one(world: World, keep: Any) -> None:
    with pytest.raises(RuntimeFault) as fault:
        observer(world).headroom(PILOT, keep=keep)
    assert fault.value.code == "QUOTA_KEEP"
