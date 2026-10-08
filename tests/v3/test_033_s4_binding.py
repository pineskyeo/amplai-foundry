"""Work 033 S4: DISPATCH_OPTIONS_BINDING and the worker's option checks (interfaces.md 3.4).

A dispatch may run only the model and effort of the activated model profile: options that differ
hold `DISPATCH_OPTIONS_BINDING` before anything is prepared, a port that takes no options holds
non-default options with `DRIVER_OPTIONS_UNSUPPORTED`, and an effort profile never runs without
its options (never substituted, spec.md Constraints). Real: store, runtime, worker coordinator,
execution loop; stand-in: the scripted host-process "container" of the rc06 rig.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.agent_drivers.cli import CodexCliDriver
from amplai_foundry.agent_drivers.ports import CliPort, DriverRegistry
from amplai_foundry.agent_drivers.protocol import EventNormalizer, SessionJournal
from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.execution.cells import (
    LEGACY_EFFORT,
    Cell,
    CellInstaller,
    DispatchOptions,
    check_binding,
    run_probe,
    store_probe,
)
from amplai_foundry.runtime.execution.codex import AUTH, OptionsCliPort, SeededCodexPort
from amplai_foundry.runtime.execution.loop import ExecutionLoop
from amplai_foundry.runtime.execution.product import AppConfig, app_capabilities
from amplai_foundry.runtime.execution.readonly_turn import TurnResult
from amplai_foundry.runtime.execution.worker import WorkCoordinator
from rc06_rig import ScriptContainer, build_rig, codex_inputs, submit

MODEL = "gpt-5.6-sol"
HIGH_ID = "codex-cli.gpt-5.6-sol.high"


class ArgvContainer(ScriptContainer):
    """The scripted agent, also recording each argv it was given."""

    def __init__(self, mode: str) -> None:
        super().__init__(mode)
        self.argvs: list[list[str]] = []

    def command(self, argv: list[str], workspace: Path, run_name: str, **kw: Any) -> list[str]:
        self.argvs.append(list(argv))
        return super().command(argv, workspace, run_name, **kw)


class NoOptionsPort(SeededCodexPort):
    """A port that takes no dispatch options (like OpenCode, RecipePort and a plain CliPort)."""

    accepts_options = False


class Turn:
    cell_id = "fake"

    def run(self, **_: Any) -> TurnResult:
        return TurnResult({"ok": True}, None, 0.0, "sha256:" + "0" * 64)


class Setup:
    """A rig with the legacy cell and a `high` effort cell of the same model, one port."""

    def __init__(self, d: Any, tmp_path: Path, *, plain_port: bool = False) -> None:
        self.d = d
        self.rig = rig = build_rig(d, tmp_path)
        self.container = ArgvContainer("right")
        driver = CodexCliDriver(
            "0.155.1",
            self.container,  # type: ignore[arg-type]
            SessionJournal(tmp_path / "journal"),
            model=MODEL, qualified=True,
        )  # fmt: skip
        self.container.driver = driver
        home = tmp_path / "scoped-codex"
        (home / ".codex").mkdir(parents=True)
        (home / AUTH).write_text('{"tokens": "original"}')
        inputs, caps = codex_inputs(tmp_path), app_capabilities("app")
        legacy = Cell("codex-cli", "codex-cli", MODEL, LEGACY_EFFORT, (), legacy=True)
        self.high = Cell(HIGH_ID, "codex-cli", MODEL, "high", (), legacy=False)
        installer = CellInstaller(d.store, d.scope)
        self.legacy_refs = installer.install(legacy, inputs, caps, probe=None)
        scratch = tmp_path / "probe"
        scratch.mkdir()
        probe = run_probe(
            d.scope, self.high, Turn(), scratch,  # type: ignore[arg-type]
            driver_version="0.155.1", image=inputs_image(tmp_path), argv=["x"],
        )  # fmt: skip
        store_probe(d.store, d.scope, probe)
        self.high_refs = installer.install(self.high, inputs, caps, probe=probe)
        assert self.legacy_refs["driver"] == self.high_refs["driver"]  # one model, one port
        registry = DriverRegistry(d.store)
        port: Any = (NoOptionsPort if plain_port else SeededCodexPort)(driver, home)
        registry.register(d.actor, self.legacy_refs["driver"], port)
        self.coordinator = WorkCoordinator(d.runtime, registry, rig.workspaces, poll_seconds=0.05)
        rig.service.install(
            AppConfig("app", rig.repo, rig.service.apps["app"].config.verifiers),
            driver_refs={"codex-cli": self.legacy_refs, HIGH_ID: self.high_refs},
            planners={HIGH_ID: rig.planner},
            cells=[self.high],
        )
        self.loop = ExecutionLoop(rig.service, self.coordinator, publisher=self.published)
        self.published_goals: list[str] = []
        self.goals = 0

    def published(self, goal_id: str) -> dict[str, Any]:
        self.published_goals.append(goal_id)
        return {"branch": "amplai/" + goal_id}

    def with_options(self, options: DispatchOptions | None) -> None:
        """Force these options on the worker, whatever the loop resolved (S8 wires the loop to
        pass ``cells.resolve_options``); the worker's binding check is what these tests prove."""
        # wrap the real execute once; a second call replaces the forced options, not stacks them
        original = self.__dict__.setdefault("_real_execute", self.coordinator.execute)

        def forced(*args: Any, **kwargs: Any) -> Any:
            return original(*args, **{**kwargs, "options": options})

        self.coordinator.execute = forced  # type: ignore[method-assign]

    def goal(self, composition: dict[str, Any] | None = None) -> str:
        self.goals += 1  # a goal per text: `submit` is idempotent on the text
        goal = submit(self.rig, f"goal {self.goals}: make value return 2")
        self.rig.service.plan(goal, composition=composition)
        self.rig.service.approve(self.rig.operator, goal)
        return goal

    def profile(self, refs: dict[str, Any]) -> dict[str, Any]:
        return {"model_profile_ref": refs["model"], "driver_profile_ref": refs["driver"]}


def inputs_image(tmp_path: Path) -> str:
    from rc06_rig import IMAGE

    return IMAGE


@pytest.fixture
def setup(deployment: Any, tmp_path: Path) -> Setup:
    return Setup(deployment, tmp_path)


def hold_code(fn: Any, *args: Any, **kwargs: Any) -> str:
    with pytest.raises(Hold) as held:
        fn(*args, **kwargs)
    return str(held.value.code)


# -- check_binding ------------------------------------------------------------------------------
def test_options_must_equal_the_profile_model_and_effort(setup: Setup) -> None:
    d = setup.d
    legacy, high = setup.profile(setup.legacy_refs), setup.profile(setup.high_refs)
    check_binding(d.store, d.scope, legacy, DispatchOptions.default(MODEL))
    check_binding(d.store, d.scope, high, DispatchOptions(MODEL, "high"))
    # provider-default is "no effort" (None), so any effort on the legacy profile differs
    for profile, options in (
        (legacy, DispatchOptions(MODEL, "high")),
        (high, DispatchOptions.default(MODEL)),
        (high, DispatchOptions(MODEL, "low")),
        (high, DispatchOptions("another-model", "high")),
        (legacy, DispatchOptions("another-model", None)),
    ):
        assert hold_code(check_binding, d.store, d.scope, profile, options) == (
            "DISPATCH_OPTIONS_BINDING"
        )


def test_the_binding_hold_names_what_differs(setup: Setup) -> None:
    d = setup.d
    with pytest.raises(Hold) as held:
        check_binding(
            d.store, d.scope, setup.profile(setup.high_refs), DispatchOptions(MODEL, "low")
        )
    assert held.value.details == {
        "profile": {"model": MODEL, "effort": "high"},
        "options": {"model": MODEL, "effort": "low"},
    }


def test_driver_options_do_not_change_the_binding(setup: Setup) -> None:
    # binding is model and effort only; the driver options are a budget-policy matter (L5)
    d = setup.d
    check_binding(
        d.store, d.scope, setup.profile(setup.high_refs),
        DispatchOptions(MODEL, "high", capture_trace=True),
    )  # fmt: skip


# -- the worker's checks, through the execution loop
def test_default_options_on_the_legacy_profile_run_exactly_as_today(setup: Setup) -> None:
    setup.with_options(DispatchOptions.default(MODEL))
    goal = setup.goal()
    assert setup.loop.run_goal(goal)["status"] == "published"
    (argv,) = setup.container.argvs
    # no effort flag: the only override is decision (C)'s web search off (2026-10-08)
    assert argv.count("-c") == 1 and argv[argv.index("-c") + 1] == 'web_search="disabled"'
    assert argv[argv.index("--model") + 1] == MODEL


def test_a_matching_effort_reaches_the_driver_argv(setup: Setup) -> None:
    setup.with_options(DispatchOptions(MODEL, "high"))
    composition = setup.rig.service.apps["app"].compositions[HIGH_ID]
    goal = setup.goal(composition)
    assert setup.loop.run_goal(goal)["status"] == "published"
    (argv,) = setup.container.argvs
    assert argv[argv.index("-c") : argv.index("-c") + 2] == ["-c", "model_reasoning_effort=high"]
    head = setup.d.store.head(setup.d.scope, "goal", goal)["data"]["profile"]
    assert head["model_profile_ref"]["id"].endswith(f"-high-{setup.high_refs['model']['id'][-12:]}")


@pytest.mark.parametrize(
    ("options", "on_high"),
    [
        (DispatchOptions(MODEL, "high"), False),  # an effort the legacy profile does not have
        (DispatchOptions(MODEL, "low"), True),  # another effort than the profile's
        (DispatchOptions.default(MODEL), True),  # no effort for an effort profile
        (DispatchOptions("another-model", "high"), True),  # another model
    ],
)
def test_options_that_differ_from_the_activated_profile_hold_before_anything_runs(
    setup: Setup, options: DispatchOptions, on_high: bool
) -> None:
    setup.with_options(options)
    composition = setup.rig.service.apps["app"].compositions[HIGH_ID] if on_high else None
    goal = setup.goal(composition)
    record = setup.loop.run_goal(goal)
    assert record["status"] == "held" and "DISPATCH_OPTIONS_BINDING" in record["reason"]
    assert setup.container.argvs == [] and setup.container.prompts == []  # nothing was prepared
    assert setup.published_goals == []


def test_an_effort_profile_never_runs_without_its_options(setup: Setup) -> None:
    # no options at the worker for an effort profile would run the model at the provider default,
    # a substitution of the effort: the worker holds
    setup.with_options(None)
    composition = setup.rig.service.apps["app"].compositions[HIGH_ID]
    goal = setup.goal(composition)
    record = setup.loop.run_goal(goal)
    assert record["status"] == "held" and "DISPATCH_OPTIONS_BINDING" in record["reason"]
    assert setup.container.argvs == []


def test_the_loop_resolves_an_effort_profile_to_its_options(setup: Setup) -> None:
    # S8: the loop passes cells.resolve_options, so the effort reaches the argv without a caller
    composition = setup.rig.service.apps["app"].compositions[HIGH_ID]
    goal = setup.goal(composition)
    assert setup.loop.run_goal(goal)["status"] == "published"
    (argv,) = setup.container.argvs
    assert argv[argv.index("-c") : argv.index("-c") + 2] == ["-c", "model_reasoning_effort=high"]


def test_no_options_on_the_legacy_profile_keeps_working(setup: Setup) -> None:
    goal = setup.goal()
    assert setup.loop.run_goal(goal)["status"] == "published"
    argv = setup.container.argvs[0]  # no effort flag; decision (C)'s web search override only
    assert argv.count("-c") == 1 and argv[argv.index("-c") + 1] == 'web_search="disabled"'


def test_a_port_without_options_support_holds_non_default_options(
    deployment: Any, tmp_path: Path
) -> None:
    setup = Setup(deployment, tmp_path, plain_port=True)
    assert getattr(CliPort, "accepts_options", False) is not True  # the real plain port
    assert issubclass(SeededCodexPort, OptionsCliPort) and SeededCodexPort.accepts_options
    setup.with_options(DispatchOptions(MODEL, "high"))
    composition = setup.rig.service.apps["app"].compositions[HIGH_ID]
    record = setup.loop.run_goal(setup.goal(composition))
    assert record["status"] == "held" and "DRIVER_OPTIONS_UNSUPPORTED" in record["reason"]
    assert setup.container.argvs == []
    # default options are no options: the plain port still runs a legacy goal
    setup.with_options(DispatchOptions.default(MODEL))
    assert setup.loop.run_goal(setup.goal())["status"] == "published"


# -- rate-limit signals keep no payload text (protocol.py, 14 Q5)
RATE_EVENT = {
    "type": "rate_limit_event", "session_id": "s", "message": "You hit the limit: secret text",
    "resets_at": 1234, "status": "limited", "flag": True, "nothing": None, "ratio": 0.5,
}  # fmt: skip


def test_a_claude_rate_limit_event_is_counted_without_its_payload() -> None:
    normalizer = EventNormalizer("claude")
    normalized = normalizer.accept(dict(RATE_EVENT))
    assert normalizer.rate_limit_events == 1
    # no field name of the payload is verified (14 Q5), so none is kept and no text is either
    assert normalized["rate_limit"] == {}
    assert "secret text" not in repr(normalized)
    other = normalizer.accept({"type": "assistant", "message": {"content": []}})
    assert "rate_limit" not in other and normalizer.rate_limit_events == 1


def test_allowlisted_rate_limit_fields_are_scalars_never_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        EventNormalizer, "RATE_LIMIT_FIELDS",
        {"claude": frozenset({"message", "resets_at", "status", "flag", "nothing", "ratio"}),
         "codex": frozenset()},
    )  # fmt: skip
    kept = EventNormalizer("claude").accept(dict(RATE_EVENT))["rate_limit"]
    assert kept == {"resets_at": 1234, "flag": True, "nothing": None, "ratio": 0.5}  # no strings


def test_a_codex_stream_has_no_rate_limit_event() -> None:
    with pytest.raises(Hold) as held:
        EventNormalizer("codex").accept(dict(RATE_EVENT))
    # the Codex usage-limit shape is 확인 필요 (Q5)
    assert held.value.code == "UNKNOWN_PROVIDER_EVENT"
