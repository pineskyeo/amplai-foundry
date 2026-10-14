"""Work 034 S1d: nightly loop control through the front agent (spec N-1..N-9, AC-N1..AC-N6; plan.md
§3.8; D-117).

The control records live in the product store (``runtime/nightly_control.py``); the night reads
them read-only and stops when it cannot (``meta_harness/nightly.py``); the tick and the read-only
status are ``runtime/meta_commands/nightly.py``. The meta deployment here is the product fixture's
deployment (as in ``test_033_s12_nightly``), the product store holding the controls is a separate
store, and the night's phase actions are the scripted fake of ``test_033_s12_nightly``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from test_033_s12_nightly import (
    CELL,
    DATE,
    SHARES,
    SNAP,
    Env,
    Fake,
    config,
    phases,
    stopped,
)
from test_rc06_local_product import product  # noqa: F401  (fixture)
from typer.testing import CliRunner

from amplai_foundry.meta_harness.nightly import (
    EVENT_APPROVAL,
    EVENT_DRIFT,
    EVENT_ENDED,
    EVENT_STARTED,
    HOLD_REASONS,
    NO_DREAM,
    NightlyRunner,
)
from amplai_foundry.runtime.cli import app
from amplai_foundry.runtime.contracts.intake import (
    INTAKE_CEILING,
    INTAKE_PERMISSIONS,
    NIGHTLY_CONTROL,
    intake_actor,
)
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.local_deployment import (
    OPERATOR_PERMISSIONS,
    IntakeAdapterEntry,
    IntakeEntry,
    LocalConfig,
    NightlyEntry,
)
from amplai_foundry.runtime.meta_commands import nightly as commands
from amplai_foundry.runtime.nightly_control import (
    EVENT,
    IDENTITY_ID,
    IDENTITY_KIND,
    KIND,
    SCHEMA,
    ControlReader,
    MetaView,
    NightlyControlService,
    NightPolicy,
    fold,
    identity_value,
    in_window,
    night_date,
    read_records,
    summary,
)
from amplai_foundry.runtime.storage.store import Scope, Store

AGENT_PERMISSIONS = INTAKE_PERMISSIONS | {NIGHTLY_CONTROL}
TONIGHT = "2026-10-15"


def local_epoch(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute).astimezone().timestamp()


# 23:00 on the 14th belongs to the night of the 15th (start_at 01:00), inside 22:00-06:00
EVENING = local_epoch(2026, 10, 14, 23)


def mark(store: Store, scope: Any, role: str) -> None:
    with store.tx() as db:
        store.cas(db, scope, IDENTITY_KIND, IDENTITY_ID, 0, role, identity_value(scope, role))


class Rig:
    """The meta deployment (``env``), a product store with the controls, and the actors."""

    def __init__(self, env: Env, root: Path) -> None:
        self.env = env
        self.root = root
        self.product = Store(root)
        self.scope = env.scope
        mark(self.product, self.scope, "product")  # as LocalProductDeployment's boot does
        subject = env.dep.config.operator_subject
        self.agent = intake_actor(subject, self.scope, "hermes", AGENT_PERMISSIONS)
        self.other_agent = intake_actor(subject, self.scope, "other", AGENT_PERMISSIONS)
        self.plain_agent = intake_actor(subject, self.scope, "hermes")
        self.operator = env.dep.operator()
        self.policy = NightPolicy(
            budget_trials=200, cells=(CELL,), shares=dict(SHARES), stop_at="07:00",
            max_hours=8, start_at="01:00", start_window=("22:00", "06:00"),
        )  # fmt: skip
        self.meta = MetaView(env.dep.local(env.dep.config.runtime_root), self.scope, self.policy)
        self.now = EVENING
        self.service = NightlyControlService(
            self.product, self.scope, self.meta, clock=lambda: self.now
        )
        self.keys = 0

    def ask(self, actor: Any, action: str, **kw: Any) -> dict[str, Any]:
        self.keys += 1
        kw.setdefault("reason", "the operator asked")
        return self.service.request(actor, kw.pop("key", f"k{self.keys}"), action=action, **kw)

    def records(self) -> list[dict[str, Any]]:
        return read_records(self.product, self.scope)


@pytest.fixture
def env(product: Any) -> Env:  # noqa: F811
    dep, _client, _token = product
    return Env(dep)


@pytest.fixture
def rig(env: Env, tmp_path: Path) -> Any:
    r = Rig(env, tmp_path / "product")
    yield r
    r.product.close()


def refused(call: Any, code: str) -> RuntimeFault:
    with pytest.raises(RuntimeFault) as caught:
        call()
    assert caught.value.code == code, caught.value.as_dict()
    return caught.value


# --- the permission and who holds it -------------------------------------------------------------


def test_nightly_control_is_in_the_ceiling_but_not_granted_by_default() -> None:
    assert NIGHTLY_CONTROL == "nightly.control"
    assert NIGHTLY_CONTROL in INTAKE_CEILING and NIGHTLY_CONTROL not in INTAKE_PERMISSIONS
    assert NIGHTLY_CONTROL in OPERATOR_PERMISSIONS
    limits = {op: {"count": 5, "window_seconds": 60} for op in ("submit", "steer", "replan",
                                                                  "cancel")}  # fmt: skip
    files = {"adapter_id": "hermes", "token_file": "t", "identity_map_file": "m"}
    # the operator names it for an adapter; a pre-S1c single intake entry does not get it
    named = IntakeAdapterEntry.model_validate(
        {**files, "permissions": ["runtime.read", NIGHTLY_CONTROL], "rate_limits": limits}
    )
    assert NIGHTLY_CONTROL in named.permissions
    legacy = IntakeEntry.model_validate(
        {"token_file": "t", "identity_map_file": "m", "rate_limits": limits}
    )
    assert set(legacy.adapters[0].permissions) == INTAKE_PERMISSIONS
    # no other nightly permission reaches a front agent (N-2): approving stays the operator's
    with pytest.raises(ValueError, match=r"nightly\.approve"):
        IntakeAdapterEntry.model_validate(
            {**files, "permissions": ["nightly.approve"], "rate_limits": limits}
        )


def test_a_front_agent_without_nightly_control_is_forbidden(rig: Rig) -> None:
    refused(lambda: rig.ask(rig.plain_agent, "stop"), "FORBIDDEN")
    assert rig.records() == []


# --- the records ---------------------------------------------------------------------------------


def test_a_stop_is_recorded_once_with_its_event_and_replays(rig: Rig) -> None:
    first = rig.ask(rig.agent, "stop", key="m-1", reason="the laptop is needed")
    again = rig.ask(rig.agent, "stop", key="m-1", reason="the laptop is needed")
    assert again == first and first["action"] == "stop" and first["dates"] == [TONIGHT]
    [record] = rig.records()
    assert record["schema"] == SCHEMA and record["control_id"] == first["control_id"]
    assert record["issued_by"] == rig.agent.wire() and record["clears"] is None
    events = [e for e in rig.product.events(rig.scope) if e["event_type"] == EVENT]
    assert len(events) == 1 and events[0]["aggregate_type"] == KIND
    # the event carries no free text (H-8): the reason stays in the record
    assert "laptop" not in json.dumps(events[0]["data"])
    assert events[0]["data"]["by"] == "front_agent" and events[0]["data"]["mark"] == "intake:hermes"
    refused(lambda: rig.ask(rig.agent, "stop", key="m-1", reason="other words"),
            "IDEMPOTENCY_CONFLICT")  # fmt: skip


def test_stop_and_skip_need_nothing_from_the_meta_deployment(rig: Rig) -> None:
    service = NightlyControlService(rig.product, rig.scope, None, clock=lambda: rig.now)
    service.request(rig.agent, "a", action="skip", dates=[TONIGHT], reason="travel")
    service.request(rig.agent, "b", action="stop", reason="now")
    with pytest.raises(Hold) as no_meta:
        service.request(rig.agent, "c", action="tighten", dates=[TONIGHT],
                        limits={"budget_trials": 5}, reason="less")  # fmt: skip
    assert no_meta.value.code == "NIGHT_CONTROL_META"


def test_dates_are_tonight_or_the_next_week(rig: Rig) -> None:
    refused(lambda: rig.ask(rig.agent, "skip", dates=["2026-10-14"]), "NIGHT_CONTROL_INPUT")
    refused(lambda: rig.ask(rig.agent, "skip", dates=["2026-10-30"]), "NIGHT_CONTROL_INPUT")
    refused(lambda: rig.ask(rig.agent, "skip", dates=["15-10-2026"]), "NIGHT_CONTROL_INPUT")
    refused(lambda: rig.ask(rig.agent, "skip"), "NIGHT_CONTROL_INPUT")
    assert rig.ask(rig.agent, "skip", dates=[TONIGHT, "2026-10-22"])["dates"] == [
        TONIGHT, "2026-10-22",
    ]  # fmt: skip


# --- AC-N2: nothing widens -----------------------------------------------------------------------


def test_a_tighten_only_lowers_the_budget(rig: Rig) -> None:
    rig.env.standing()
    for budget in (200, 250):
        with pytest.raises(Hold) as widen:
            rig.ask(rig.agent, "tighten", dates=[TONIGHT], limits={"budget_trials": budget})
        assert widen.value.code == "NIGHT_CONTROL_WIDEN"
    rig.ask(rig.agent, "tighten", dates=[TONIGHT], limits={"budget_trials": 50})
    with pytest.raises(Hold) as looser:  # above the tighten already in force
        rig.ask(rig.agent, "tighten", dates=[TONIGHT], limits={"budget_trials": 80})
    assert looser.value.code == "NIGHT_CONTROL_WIDEN"
    rig.ask(rig.operator, "tighten", dates=[TONIGHT], limits={"budget_trials": 20})
    assert fold(rig.records(), TONIGHT).budget_trials == 20


def test_a_tighten_only_brings_the_stop_time_earlier(rig: Rig) -> None:
    # tonight starts at 01:00 and ends at 07:00 (stop_at, within max_hours 8). A stop time at or
    # after that end, or one that falls on the next day (00:30, 23:30), changes nothing: refused,
    # with the end in force (PR #95 review P3)
    for later in ("08:00", "07:00", "12:00", "00:30", "23:30"):
        with pytest.raises(Hold) as widen:
            rig.ask(rig.agent, "tighten", dates=[TONIGHT], limits={"stop_at": later})
        assert widen.value.code == "NIGHT_CONTROL_WIDEN"
        assert widen.value.details["effective"].startswith(TONIGHT + "T07:00")
    rig.ask(rig.agent, "tighten", dates=[TONIGHT], limits={"stop_at": "05:00"})
    with pytest.raises(Hold):
        rig.ask(rig.agent, "tighten", dates=[TONIGHT], limits={"stop_at": "06:00"})
    rig.ask(rig.agent, "tighten", dates=[TONIGHT], limits={"stop_at": "04:00"})
    assert fold(rig.records(), TONIGHT).stop_at == ("05:00", "04:00")


def test_a_tighten_is_well_formed(rig: Rig) -> None:
    for limits in ({}, {"budget_trials": -1}, {"budget_trials": 1.5}, {"stop_at": "7:00"},
                   {"max_tokens": 5}):  # fmt: skip
        refused(lambda lim=limits: rig.ask(rig.agent, "tighten", dates=[TONIGHT], limits=lim),
                "NIGHT_CONTROL_INPUT")  # fmt: skip
    refused(lambda: rig.ask(rig.agent, "skip", dates=[TONIGHT], limits={"budget_trials": 1}),
            "NIGHT_CONTROL_INPUT")  # fmt: skip


def test_the_front_agent_clears_only_its_own_skip_or_tighten_before_the_night(rig: Rig) -> None:
    mine = rig.ask(rig.agent, "skip", dates=[TONIGHT])
    theirs = rig.ask(rig.operator, "skip", dates=["2026-10-16"])
    other = rig.ask(rig.other_agent, "skip", dates=["2026-10-17"])
    stop = rig.ask(rig.agent, "stop", dates=["2026-10-18"])
    # AC-N2: never the operator's, never another adapter's, never its stop
    refused(lambda: rig.ask(rig.agent, "clear", clears=theirs["control_id"]), "FORBIDDEN")
    refused(lambda: rig.ask(rig.agent, "clear", clears=other["control_id"]), "FORBIDDEN")
    refused(lambda: rig.ask(rig.agent, "clear", clears=stop["control_id"]), "FORBIDDEN")
    refused(lambda: rig.ask(rig.agent, "clear", clears="nightctl-nope"), "NOT_FOUND")
    cleared = rig.ask(rig.agent, "clear", clears=mine["control_id"])
    assert cleared["dates"] == [TONIGHT]
    assert fold(rig.records(), TONIGHT).held is False
    refused(lambda: rig.ask(rig.agent, "clear", clears=mine["control_id"]), "NOT_FOUND")
    # the operator clears anyone's
    rig.ask(rig.operator, "clear", clears=other["control_id"])
    rig.ask(rig.operator, "clear", clears=stop["control_id"])
    assert fold(rig.records(), "2026-10-17").held is False
    assert fold(rig.records(), "2026-10-16").operator_held is True


def test_the_front_agent_cannot_clear_once_its_night_started(rig: Rig) -> None:
    rig.now = local_epoch(2026, 10, 1, 0, 30)  # the night of DATE
    rig.env.standing()
    tighten = rig.ask(rig.agent, "tighten", dates=[DATE], limits={"budget_trials": 150})
    rig.env.runner().run(DATE)  # the night ran
    with pytest.raises(Hold) as started:
        rig.ask(rig.agent, "clear", clears=tighten["control_id"])
    assert started.value.code == "NIGHT_CONTROL_STARTED"
    with pytest.raises(Hold) as ended:  # and no tighten for a night that ended
        rig.ask(rig.agent, "tighten", dates=[DATE], limits={"budget_trials": 10})
    assert ended.value.code == "NIGHT_CONTROL_ENDED"


def test_a_clear_by_another_front_agent_is_ignored_when_folding(rig: Rig) -> None:
    """A record that says it clears another's control does not count (fold re-checks)."""
    mine = rig.ask(rig.agent, "skip", dates=[TONIGHT])
    forged = {
        "schema": SCHEMA, "scope": rig.scope.wire(), "control_id": "nightctl-forged",
        "action": "clear", "dates": [TONIGHT], "limits": {}, "issued_by": rig.other_agent.wire(),
        "reason": "x", "at": "2099-01-01T00:00:00Z", "clears": mine["control_id"],
    }  # fmt: skip
    with rig.product.tx() as db:
        rig.product.put(db, rig.scope, KIND, "nightctl-forged", 1, forged)
    assert fold(rig.records(), TONIGHT).stop == "skipped"


def test_the_strictest_stop_reason_wins(rig: Rig) -> None:
    rig.ask(rig.agent, "skip", dates=[TONIGHT])
    assert fold(rig.records(), TONIGHT).stop == "skipped"
    rig.ask(rig.agent, "stop", dates=[TONIGHT])
    assert fold(rig.records(), TONIGHT).stop == "front_agent_stop"
    rig.ask(rig.operator, "stop", dates=[TONIGHT])
    assert fold(rig.records(), TONIGHT).stop == "operator_stop"


# --- AC-N6: the front agent's start (N-9) --------------------------------------------------------


def start_refused(rig: Rig, condition: str, actor: Any = None) -> None:
    with pytest.raises(Hold) as caught:
        rig.ask(actor or rig.agent, "start")
    assert caught.value.code == "NIGHT_START_REFUSED"
    assert caught.value.details["condition"] == condition, caught.value.details


def test_a_start_inside_the_approval_and_window_is_recorded_for_tonight(rig: Rig) -> None:
    rig.env.standing()
    done = rig.ask(rig.agent, "start", reason="the operator is away tonight")
    assert done["dates"] == [TONIGHT]
    assert [r["control_id"] for r in fold(rig.records(), TONIGHT).starts] == [done["control_id"]]
    start_refused(rig, "already_requested")  # the frequency limit: one start per night
    refused(lambda: rig.ask(rig.agent, "start", dates=["2026-10-16"]), "NIGHT_START_REFUSED")


def test_a_start_without_a_standing_approval_is_refused(rig: Rig) -> None:
    start_refused(rig, "no_standing_approval")


def test_a_start_outside_the_standing_policy_is_refused(rig: Rig, env: Env) -> None:
    env.standing(budget_trials=200)
    rig.policy = NightPolicy(**{**rig.policy.__dict__, "budget_trials": 500})
    rig.meta.policy = rig.policy  # the configured night asks more than the approval grants
    start_refused(rig, "budget_trials")


def test_a_start_outside_the_window_or_without_one_is_refused(rig: Rig, env: Env) -> None:
    env.standing()
    rig.now = local_epoch(2026, 10, 14, 15)
    start_refused(rig, "outside_window")
    rig.now = EVENING
    rig.meta.policy = NightPolicy(**{**rig.policy.__dict__, "start_window": None})
    start_refused(rig, "no_start_window")


def test_a_start_while_a_stop_or_skip_holds_the_night_is_refused(rig: Rig, env: Env) -> None:
    env.standing()
    skip = rig.ask(rig.operator, "skip", dates=[TONIGHT])
    start_refused(rig, "held")
    rig.ask(rig.operator, "clear", clears=skip["control_id"])
    rig.ask(rig.agent, "start")


def test_a_start_for_a_night_that_already_ran_is_refused(rig: Rig, env: Env) -> None:
    env.standing()
    rig.now = local_epoch(2026, 10, 1, 0, 30)
    rig.meta.policy = NightPolicy(**{**rig.policy.__dict__, "start_window": ("00:00", "06:00")})
    env.runner().run(DATE)
    start_refused(rig, "already_ran")


def test_night_dates_and_windows() -> None:
    assert night_date(local_epoch(2026, 10, 14, 23), "01:00") == "2026-10-15"
    assert night_date(local_epoch(2026, 10, 15, 3), "01:00") == "2026-10-15"
    assert night_date(local_epoch(2026, 10, 15, 13), "01:00") == "2026-10-16"
    assert night_date(local_epoch(2026, 10, 14, 23), "22:00") == "2026-10-14"
    assert night_date(local_epoch(2026, 10, 15, 3), "22:00") == "2026-10-14"
    assert in_window(local_epoch(2026, 10, 14, 23), ("22:00", "06:00"))
    assert in_window(local_epoch(2026, 10, 15, 5, 59), ("22:00", "06:00"))
    assert not in_window(local_epoch(2026, 10, 15, 6), ("22:00", "06:00"))
    assert in_window(local_epoch(2026, 10, 15, 1), ("00:00", "06:00"))


# --- the night: AC-N1, AC-N3, AC-N5 --------------------------------------------------------------


def controlled(env: Env, root: Path, controls: Any = None) -> NightlyRunner:
    """A runner of ``env`` whose controls are the product store at ``root`` (read-only)."""
    env.standing()
    runner = NightlyRunner(env.dep, config(env.clock), clock=env.clock, backend=env.fake,
                           controls=controls or ControlReader(root, env.scope))  # fmt: skip
    env.fake.runner = runner
    return runner


def test_ac_n1_a_front_agent_stop_mid_night_stops_at_the_next_trial_guard(rig: Rig) -> None:
    env = rig.env
    rig.now = local_epoch(2026, 10, 1, 0, 30)

    def stop(fake: Fake, cid: str, tasks: int) -> None:
        if fake.search_n == 2:  # after the first unit's check, before this unit's own guard
            rig.ask(rig.agent, "stop", dates=[DATE], reason="I need the subscription")

    env.fake.before_search = stop
    runner = controlled(env, rig.root)
    held = stopped(env, runner)
    assert held.details["reason"] == "front_agent_stop"
    assert len(env.fake.args("search_run")) == 2  # the second unit's guard refused it
    data = env.head()["data"]
    assert data["stopped"] == "front_agent_stop" and data["trials"] == 5 + 12 + 4
    assert data["controls"]["applied"] == [rig.records()[0]["control_id"]]
    assert phases(data)["dreaming"] == "skipped" and "dream" not in env.fake.names()
    assert env.head()["state"] == "stopped"
    ended = [e for e in env.store.events(env.scope) if e["event_type"] == EVENT_ENDED]
    assert ended[-1]["data"] == {"date": DATE, "state": "stopped", "reason": "front_agent_stop",
                                 "trials": 21}  # fmt: skip


def test_ac_n3_a_skipped_night_stops_at_preflight(rig: Rig) -> None:
    rig.now = local_epoch(2026, 10, 1, 0, 30)
    rig.ask(rig.agent, "skip", dates=[DATE], reason="travel")
    held = stopped(rig.env, controlled(rig.env, rig.root))
    assert held.details["reason"] == "skipped"
    data = rig.env.head()["data"]
    assert phases(data)["preflight"] == "stopped" and data["trials"] == 0
    assert "preflight" not in rig.env.fake.names()  # nothing of the backend ran


def test_ac_n5_unreadable_controls_stop_the_night_at_preflight(env: Env, tmp_path: Path) -> None:
    held = stopped(env, controlled(env, tmp_path / "no-such-store"))
    assert held.details["reason"] == "control_unreadable"
    data = env.head()["data"]
    assert data["controls"]["unreadable"] == "NIGHT_CONTROL_UNREADABLE"
    assert data["trials"] == 0


def test_ac_n5_controls_that_become_unreadable_stop_the_night(rig: Rig) -> None:
    reader = ControlReader(rig.root, rig.scope)
    broken = {"now": False}

    def read(date: str, started_at: str | None = None) -> Any:
        if broken["now"]:
            raise Hold("NIGHT_CONTROL_UNREADABLE", "gone")
        return reader(date, started_at)

    def breaks(fake: Fake, cid: str, tasks: int) -> None:
        broken["now"] = fake.search_n == 2 or broken["now"]

    rig.env.fake.before_search = breaks
    held = stopped(rig.env, controlled(rig.env, rig.root, read))
    assert held.details["reason"] == "control_unreadable"
    assert rig.env.head()["data"]["trials"] == 5 + 12 + 4


def test_a_malformed_control_record_is_unreadable(rig: Rig) -> None:
    with rig.product.tx() as db:
        rig.product.put(db, rig.scope, KIND, "nightctl-bad", 1, {"action": "stop"})
    with pytest.raises(Hold) as caught:
        ControlReader(rig.root, rig.scope)(DATE)
    assert caught.value.code == "NIGHT_CONTROL_UNREADABLE"


def test_a_tightened_budget_ends_the_night_early_as_a_budget_end(rig: Rig) -> None:
    rig.now = local_epoch(2026, 10, 1, 0, 30)
    rig.env.standing()
    done = rig.ask(rig.agent, "tighten", dates=[DATE], limits={"budget_trials": 30})
    runner = controlled(rig.env, rig.root)
    runner.run(DATE)  # a budget end is a normal end
    data = rig.env.head()["data"]
    assert data["controls"]["budget_trials"] == 30 and data["controls"]["applied"] == [
        done["control_id"]
    ]
    assert data["trials"] <= 30 and data["stopped"] == "budget"


def test_a_tightened_stop_time_ends_the_night_earlier(rig: Rig) -> None:
    env = rig.env
    rig.now = local_epoch(2026, 10, 1, 0, 30)
    cfg = config(env.clock)
    later = datetime.fromtimestamp(env.clock.t + 2 * 3600)
    sooner = later.strftime("%H:%M")
    # the night's scheduled start is the test clock's time of day, so +2 h is before its end
    start_at = datetime.fromtimestamp(env.clock.t).strftime("%H:%M")
    rig.meta.policy = NightPolicy(
        **{**rig.policy.__dict__, "stop_at": cfg.stop_at, "start_at": start_at}
    )
    rig.env.standing()
    rig.ask(rig.agent, "tighten", dates=[DATE], limits={"stop_at": sooner})
    runner = NightlyRunner(env.dep, cfg, clock=env.clock, backend=env.fake,
                           controls=ControlReader(rig.root, env.scope))  # fmt: skip
    env.fake.runner = runner

    def jump(fake: Fake, cid: str, tasks: int) -> None:
        if fake.search_n == 1:
            env.clock.advance(3 * 3600)  # past the tightened stop, before the configured one

    env.fake.after_search = jump
    held = stopped(env, runner)
    assert held.details["reason"] == "stop_at"
    data = env.head()["data"]
    assert data["controls"]["stop_at"].startswith(later.strftime("%Y-%m-%dT%H:%M"))


def test_an_applied_tighten_stays_for_the_night_after_a_clear(rig: Rig) -> None:
    env = rig.env
    rig.now = local_epoch(2026, 10, 1, 0, 30)
    env.standing()
    done = rig.ask(rig.operator, "tighten", dates=[DATE], limits={"budget_trials": 30})

    def clear(fake: Fake, cid: str, tasks: int) -> None:
        if fake.search_n == 1:
            rig.ask(rig.operator, "clear", clears=done["control_id"])

    env.fake.before_search = clear
    controlled(env, rig.root).run(DATE)
    assert env.head()["data"]["trials"] <= 30


def test_the_control_stop_reasons_end_with_a_hold_and_no_dream() -> None:
    for reason in ("front_agent_stop", "operator_stop", "skipped", "control_unreadable"):
        assert reason in HOLD_REASONS and reason in NO_DREAM


def test_a_night_without_a_control_store_reads_no_control(env: Env) -> None:
    runner = env.ready()
    runner.run(DATE)
    data = env.head()["data"]
    assert data["controls"] == {"applied": [], "budget_trials": None, "stop_at": None,
                                "trigger": None}  # fmt: skip


def test_n6_a_night_writes_its_start_and_end_events_and_a_dry_run_none(env: Env) -> None:
    runner = env.ready()
    runner.run("2026-10-02", dry_run=True)
    assert [e for e in env.store.events(env.scope) if e["aggregate_type"] == "nightly-run"] == []
    runner.run(DATE)
    mine = [e for e in env.store.events(env.scope) if e["aggregate_type"] == "nightly-run"]
    # the fake queues a promising candidate for the operator: an approval waits
    assert [e["event_type"] for e in mine] == [EVENT_STARTED, EVENT_ENDED, EVENT_APPROVAL]
    assert all(e["aggregate_id"] == f"night-{DATE}" for e in mine)
    assert mine[1]["data"]["state"] == "finished"
    assert mine[1]["data"]["reason"] == env.head()["data"]["stopped"]
    queued = len(env.head()["data"]["queued_confirmations"])
    assert mine[2]["data"] == {"date": DATE, "queued": queued} and queued > 0


def test_n6_a_drifted_night_writes_a_drift_event_with_the_cells(env: Env) -> None:
    env.fake.snaps[CELL] = {
        "current": {**SNAP, "image": "sha256:image-2"}, "calibrated": SNAP,
        "band": {"passes": 8, "runs": 10}, "calibrated_at": "2026-09-30T00:00:00Z",
    }  # fmt: skip
    stopped(env, env.ready())
    drift = [e for e in env.store.events(env.scope) if e["event_type"] == EVENT_DRIFT]
    assert [e["data"] for e in drift] == [{"date": DATE, "cells": [CELL]}]


# --- N-5, AC-N4: status and the front agent's summary while a night holds the store ---------------


def meta_config(env: Env, tmp_path: Path, product_root: Path | None, **nightly: Any) -> Path:
    """A meta deployment config on ``env``'s store (absolute runtime root) with meta.nightly."""
    value = json.loads(env.dep.path.read_bytes())
    value["runtime_root"] = str(env.dep.local(env.dep.config.runtime_root))
    entry: dict[str, Any] = {"cells": [CELL], "shares": dict(SHARES), "budget_trials": 200,
                             "stop_at": "07:00", "start_at": "01:00", **nightly}  # fmt: skip
    value["meta"] = {"nightly": entry}
    if product_root is not None:
        value["meta"]["control_store"] = {"runtime_root": str(product_root),
                                          "scope": env.scope.wire()}  # fmt: skip
    LocalConfig.model_validate(value)
    path = tmp_path / "meta" / "local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def test_ac_n4_status_reads_while_another_process_owns_the_store(rig: Rig, tmp_path: Path) -> None:
    env = rig.env
    root = env.dep.local(env.dep.config.runtime_root)
    with pytest.raises(Hold) as owned:  # the deployment holds the meta store, as a night does
        Store(root)
    assert owned.value.code == "ACTIVE_OWNER"
    env.standing()
    rig.now = local_epoch(2026, 10, 1, 0, 30)
    rig.ask(rig.agent, "tighten", dates=[DATE], limits={"budget_trials": 10})
    env.runner().run(DATE)
    path = meta_config(env, tmp_path, rig.root)
    shown = commands.status_readonly(path, DATE)
    assert shown["night"]["state"] == "finished" and shown["standing"]["budget_trials"] == 200
    assert shown["controls"]["budget_trials"] == 10
    result = CliRunner().invoke(app, ["meta", "nightly", "status", "--config", str(path),
                                      "--date", DATE])  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "finished" in result.output


def test_status_says_when_the_controls_cannot_be_read(env: Env, tmp_path: Path) -> None:
    path = meta_config(env, tmp_path, tmp_path / "missing")
    shown = commands.status_readonly(path, DATE)
    assert shown["controls"] == {"unreadable": "NIGHT_CONTROL_UNREADABLE"}
    assert "controls" not in commands.status_readonly(meta_config(env, tmp_path, None), DATE)


def test_the_front_agent_summary_is_a_fixed_schema_without_text(rig: Rig) -> None:
    env = rig.env
    rig.now = local_epoch(2026, 10, 1, 0, 30)
    rig.ask(rig.agent, "stop", dates=[DATE], reason="private words here")
    out = summary(rig.meta, DATE, rig.records())
    assert set(out) == {"schema", "date", "standing", "night", "controls", "controls_unreadable"}
    assert out["night"] is None and out["standing"] == {"current": False, "valid_until": None}
    assert [c["by"] for c in out["controls"]] == ["front_agent"]
    assert "private" not in json.dumps(out)
    held = stopped(env, controlled(env, rig.root))
    assert held.details["reason"] == "front_agent_stop"
    out = summary(rig.meta, DATE, None)
    assert out["controls_unreadable"] is True and out["controls"] is None
    assert out["standing"]["current"] is True
    assert out["night"] == {"state": "stopped", "phase": "dashboard", "trials": 0,
                            "stopped": "front_agent_stop", "queued_confirmations": 0,
                            "reconcile_pending": 0}  # fmt: skip


# --- N-1: the tick -------------------------------------------------------------------------------


class Starts:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error = error

    def __call__(self, date: str, trigger: dict[str, Any]) -> Any:
        self.calls.append((date, trigger))
        if self.error is not None:
            raise self.error
        return {"id": f"night-{date}"}


def test_the_tick_starts_the_night_at_its_scheduled_start(env: Env, tmp_path: Path) -> None:
    env.standing()
    path = meta_config(env, tmp_path, None)
    starts = Starts()
    out = commands.tick(path, clock=lambda: local_epoch(2026, 10, 15, 1, 5), start=starts)
    assert out["tick"] == "started" and starts.calls == [
        (TONIGHT, {"by": "schedule", "start_at": "01:00"})
    ]
    # before the start, after the stop: nothing
    for moment in (local_epoch(2026, 10, 15, 0, 55), local_epoch(2026, 10, 15, 7, 0)):
        assert commands.tick(path, clock=lambda m=moment: m, start=starts)["tick"] == "idle"
    assert len(starts.calls) == 1


def test_the_tick_ends_at_once_when_the_night_ran_or_the_store_is_held(
    env: Env, tmp_path: Path
) -> None:
    env.standing()
    path = meta_config(env, tmp_path, None)
    busy = Starts(commands.TickBusy())
    clock = local_epoch(2026, 10, 1, 1, 5)
    assert commands.tick(path, clock=lambda: clock, start=busy)["tick"] == "busy"
    # only the open is busy: an ACTIVE_OWNER from inside the run is the run's result (P3)
    inside = Starts(Hold("ACTIVE_OWNER", "Another control plane owns this local store"))
    with pytest.raises(Hold) as raised:
        commands.tick(path, clock=lambda: clock, start=inside)
    assert raised.value.code == "ACTIVE_OWNER"
    env.runner().run(DATE)
    starts = Starts()
    out = commands.tick(path, clock=lambda: clock, start=starts)
    assert out == {"tick": "ran", "date": DATE, "state": "finished"} and starts.calls == []


def test_the_tick_without_a_standing_approval_starts_nothing(env: Env, tmp_path: Path) -> None:
    starts = Starts()
    out = commands.tick(meta_config(env, tmp_path, None),
                        clock=lambda: local_epoch(2026, 10, 15, 1, 5), start=starts)  # fmt: skip
    assert out["reason"] == "no_standing_approval" and starts.calls == []


def test_ac_n6_the_tick_starts_a_valid_start_request(rig: Rig, tmp_path: Path) -> None:
    env = rig.env
    env.standing()
    window = {"start": "22:00", "end": "06:00"}
    path = meta_config(env, tmp_path, rig.root, agent_start_window=window)
    starts = Starts()
    assert commands.tick(path, clock=lambda: EVENING, start=starts) == {
        "tick": "idle", "date": TONIGHT,
    }  # fmt: skip
    done = rig.ask(rig.agent, "start")
    out = commands.tick(path, clock=lambda: EVENING, start=starts)
    assert out["tick"] == "started" and starts.calls == [
        (
            TONIGHT,
            {"by": "start_request", "control_id": done["control_id"], "issued_by": "intake:hermes"},
        )
    ]
    # checked again right before the start: an operator skip since the request wins
    rig.ask(rig.operator, "skip", dates=[TONIGHT])
    out = commands.tick(path, clock=lambda: EVENING, start=starts)
    assert out["start_refused"] == "held" and len(starts.calls) == 1
    # outside the window the request has no effect
    later = local_epoch(2026, 10, 15, 11)
    out = commands.tick(path, clock=lambda: later, start=starts)
    assert out["tick"] == "idle" and len(starts.calls) == 1


def test_the_tick_does_not_start_on_unreadable_controls(env: Env, tmp_path: Path) -> None:
    env.standing()
    path = meta_config(env, tmp_path, tmp_path / "missing",
                       agent_start_window={"start": "22:00", "end": "06:00"})  # fmt: skip
    starts = Starts()
    out = commands.tick(path, clock=lambda: EVENING, start=starts)
    assert out["reason"] == "NIGHT_CONTROL_UNREADABLE" and starts.calls == []


def test_the_nightly_entry_defaults_and_window_shape() -> None:
    entry = NightlyEntry()
    assert entry.start_at == "01:00" and entry.agent_start_window is None
    with pytest.raises(ValueError):
        NightlyEntry.model_validate({"agent_start_window": {"start": "22:00", "end": "22:00"}})
    with pytest.raises(ValueError):
        NightlyEntry.model_validate({"start_at": "25:00"})


# --- PR #95 review -------------------------------------------------------------------------------


def test_p2_1_every_deployment_boot_marks_its_store(env: Env) -> None:
    head = env.store.head(env.scope, IDENTITY_KIND, IDENTITY_ID)
    # the fixture's config has no meta.nightly: a product deployment
    assert head["data"] == identity_value(env.scope, "product") and head["state"] == "product"


@pytest.mark.parametrize("case", ["wrong_scope", "meta_role", "unmarked", "meta_root"])
def test_p2_1_the_reader_proves_it_opened_this_scopes_product_store(
    env: Env, tmp_path: Path, case: str
) -> None:
    root = tmp_path / "other"
    with Store(root) as other:
        if case == "wrong_scope":
            mark(other, Scope("local", "another-project"), "product")
        elif case == "meta_role":
            mark(other, env.scope, "meta")
    if case == "meta_root":  # control_store names the meta deployment's own store
        root = env.dep.local(env.dep.config.runtime_root)
    reader = ControlReader(root, env.scope, meta_root=env.dep.local(env.dep.config.runtime_root))
    with pytest.raises(Hold) as caught:
        reader(DATE)
    assert caught.value.code == "NIGHT_CONTROL_UNREADABLE"
    assert caught.value.details == {"code": "NIGHT_CONTROL_STORE"}
    # and the night stops on it
    held = stopped(env, controlled(env, root, reader))
    assert held.details["reason"] == "control_unreadable"


def test_p2_1_a_control_store_on_the_meta_root_is_unreadable_from_the_config(
    env: Env, tmp_path: Path
) -> None:
    path = meta_config(env, tmp_path, env.dep.local(env.dep.config.runtime_root))
    assert commands.status_readonly(path, DATE)["controls"] == {
        "unreadable": "NIGHT_CONTROL_UNREADABLE"
    }


def test_p2_2_a_stop_the_morning_after_resolves_to_the_next_night(rig: Rig) -> None:
    rig.env.standing()
    rig.env.runner().run(DATE)  # the night of DATE ran and ended
    rig.now = local_epoch(2026, 10, 1, 9)  # the morning after: still the night of DATE
    done = rig.ask(rig.agent, "stop", reason="not tonight either")
    assert done["dates"] == ["2026-10-02"] and done["requested_dates"] is None
    with pytest.raises(Hold) as ended:  # a named night that ended is refused
        rig.ask(rig.agent, "skip", dates=[DATE])
    assert ended.value.code == "NIGHT_CONTROL_ENDED"
    assert ended.value.details == {"dates": [DATE]}


def test_p3_fold_ignores_a_front_agent_clear_the_service_would_refuse(rig: Rig) -> None:
    stop = rig.ask(rig.agent, "stop", dates=[TONIGHT])
    skip = rig.ask(rig.agent, "skip", dates=["2026-10-16"])

    def forge(control_id: str, at: str, date: str) -> None:
        value = {
            "schema": SCHEMA, "scope": rig.scope.wire(), "control_id": "nightctl-x" + at[:4],
            "action": "clear", "dates": [date], "limits": {}, "issued_by": rig.agent.wire(),
            "reason": "x", "at": at, "clears": control_id,
        }  # fmt: skip
        with rig.product.tx() as db:
            rig.product.put(db, rig.scope, KIND, value["control_id"], 1, value)

    forge(stop["control_id"], "2026-10-14T23:30:00Z", TONIGHT)  # its own stop: never
    assert fold(rig.records(), TONIGHT).stop == "front_agent_stop"
    forge(skip["control_id"], "2027-01-01T00:00:00Z", "2026-10-16")  # after the night started
    assert fold(rig.records(), "2026-10-16", "2026-10-15T16:00:00Z").stop == "skipped"
    assert fold(rig.records(), "2026-10-16").stop is None  # before any start it counts


def test_p3_start_requests_per_night_count_cleared_ones(rig: Rig) -> None:
    rig.env.standing()
    first = rig.ask(rig.agent, "start")
    rig.ask(rig.agent, "clear", clears=first["control_id"])
    start_refused(rig, "frequency")
    rig.meta.policy = NightPolicy(**{**rig.policy.__dict__, "start_limit": 2})
    rig.ask(rig.agent, "start")
    assert NightlyEntry().agent_start_limit == 1


def test_p3_the_started_event_names_the_trigger(env: Env) -> None:
    runner = env.ready()
    runner.trigger = {"by": "start_request", "control_id": "nightctl-1",
                      "issued_by": "intake:hermes"}  # fmt: skip
    runner.run(DATE)
    [started] = [e for e in env.store.events(env.scope) if e["event_type"] == EVENT_STARTED]
    assert started["data"] == {"date": DATE, "trigger": runner.trigger}


def test_p3_the_tick_is_busy_when_the_meta_store_cannot_be_opened(env: Env, tmp_path: Path) -> None:
    """The real open (no injected start): the fixture's deployment owns the store, as a
    calibration or a running night would."""
    env.standing()
    path = meta_config(env, tmp_path, None)
    out = commands.tick(path, clock=lambda: local_epoch(2026, 10, 15, 1, 5))
    assert out == {"tick": "busy", "date": TONIGHT}
