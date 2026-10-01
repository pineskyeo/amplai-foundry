"""Work 033 S10: judge connectors, calls and qualification (interfaces.md §6.6, §6.7, §2.6, IC-14,
IC-21; `plan.md` §10.4, §10.5).

Contract read: §6.6 (question types, `LlmCellJudge` over a read-only turn, `JevJudge` stub, the
`JudgeService.ask` refusals, the choice rule over qualified judges), §6.7 (qualification: accuracy
with a Wilson interval, Brier, 10-bin ECE, repeat agreement, tokens and latency, default thresholds
0.8 / 0.9 / 0.1, immutable records), §2.6 records, IC-14 (`JUDGE_WORKSPACE`), IC-21 (a judge turn is
auxiliary), §3.7 API and the §14 Q9 unknown (Jev access is not known; nothing here calls it).

Real: store, `ArtifactStore`, `JudgeService`, `JudgeQualifier`, `LlmCellJudge`, `JevJudge`,
`NoJudge`, `Decider` with a judge estimator, the real `LocalExecutionService.plan` at L1. Stand-ins
(named): the read-only turn of a cell is a scripted `JudgeTurn` (no driver, no provider, no
network); a "model" judge in the qualification tests is a scripted connector that answers from the
text of its state. Nothing is sent to Jev.
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_033_s10_deciders import (
    CELL,
    QUESTION_DRAFT,
    Env,
    Ref,
    build_world,
    candidate,
    decisions_of,
    env_of,
    fault,
    hold,
    layers_of,
    only,
)

from amplai_foundry.meta_harness import judges
from amplai_foundry.meta_harness.deciders import AMBIGUITY_QUESTION, Decider, DecisionContext
from amplai_foundry.meta_harness.judges import (
    DEFAULT_THRESHOLDS,
    JevConfig,
    JevJudge,
    JudgeAnswer,
    JudgeQualifier,
    JudgeQuestion,
    JudgeService,
    JudgeState,
    LlmCellJudge,
    NoJudge,
    admit_state,
    choose_qualified,
    current_qualification,
    qualifications,
    qualified_chooser,
    write_label_set,
)
from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.evidence.cas import ArtifactStore
from amplai_foundry.runtime.execution import policies
from amplai_foundry.runtime.execution.readonly_turn import TurnResult
from amplai_foundry.runtime.execution.strategy_runner import AuxLedger

YES = JudgeQuestion("q", "yes_no", "Is the goal ambiguous?")
CHOICE = JudgeQuestion("q", "choice", "Which kind of task is it?", choices=("bug", "feature"))
SCORE = JudgeQuestion("q", "score", "How hard is it?", scale=(0.0, 10.0))
STATE = JudgeState(text="make value return 2", data_class="internal")


# ======================================================================================
# stand-ins
# ======================================================================================
class JudgeTurn:
    """A scripted read-only turn of a cell (the `ReadOnlyTurn` protocol): records what it was
    given and answers `reply` (a dict, or a callable of the prompt)."""

    def __init__(
        self,
        cell_id: str = "judge-cell",
        *,
        model: str | None = "model-1",
        effort: str | None = "high",
        reply: Any = None,
        usage: tuple[int, int] | None = (30, 12),
    ) -> None:
        self.cell_id, self.model, self.effort = cell_id, model, effort
        self.reply, self.usage = reply, usage
        self.calls: list[dict[str, Any]] = []

    def run(
        self, *, prompt: str, schema: dict[str, Any], workspace: Path, mounts: Any = None
    ) -> TurnResult:
        where = Path(workspace)
        self.calls.append(
            {
                "prompt": prompt,
                "schema": schema,
                "workspace": where,
                "mounts": mounts,
                "existed": where.is_dir(),
                "entries": sorted(os.listdir(where)) if where.is_dir() else [],
            }
        )
        reply = self.reply(prompt) if callable(self.reply) else copy.deepcopy(self.reply)
        counted = (
            None
            if self.usage is None
            else {"input_tokens": self.usage[0], "output_tokens": self.usage[1]}
        )
        return TurnResult(reply, counted, 0.0, "sha256:" + "0" * 64)


def answer(question_id: str, value: Any, probability: float | None = 0.9) -> dict[str, Any]:
    return {"question_id": question_id, "value": value, "probability": probability}


class Scripted:
    """A `JudgeConnector` that answers from the text of its state: `decide(text, nth)` is the
    value and probability of its nth ask of that text (the qualification tests need a judge with
    a known accuracy, calibration and stability)."""

    def __init__(
        self,
        judge_id: str = "scripted.a",
        version: str = "v1",
        *,
        decide: Callable[[str, int], tuple[Any, float | None]],
        tokens: int | None = 50,
        classes: frozenset[str] = frozenset({"public", "internal"}),
    ) -> None:
        self.judge_id, self.version, self.data_classes_allowed = judge_id, version, classes
        self.decide, self.tokens = decide, tokens
        self.asked: dict[str, int] = {}
        self.last_usage: dict[str, Any] | None = None

    def ask(self, state: JudgeState, questions: list[JudgeQuestion]) -> list[JudgeAnswer]:
        nth = self.asked[state.text] = self.asked.get(state.text, 0) + 1
        value, probability = self.decide(state.text, nth)
        self.last_usage = (
            None if self.tokens is None else {"input_tokens": self.tokens, "output_tokens": 0}
        )
        return [JudgeAnswer(q.question_id, value, probability, True) for q in questions]


def label_text(text: str) -> bool:
    return text.endswith("Y")


def exact(text: str, nth: int) -> tuple[Any, float | None]:
    return label_text(text), 0.95 if label_text(text) else 0.05


def items(n: int = 40) -> list[tuple[str, bool]]:
    """n yes/no items alternating Y and N; the state text carries its label."""
    return [(f"case {i} {'Y' if i % 2 == 0 else 'N'}", i % 2 == 0) for i in range(n)]


def label_set(
    env: Env,
    data: list[tuple[str, Any]],
    question_type: str = "yes_no",
    *,
    data_class: str = "internal",
    question: dict[str, Any] | None = None,
) -> Ref:
    artifacts = ArtifactStore(env.store)
    entries = []
    for i, (text, label) in enumerate(data):
        entries.append(
            {
                "item_id": f"i{i}",
                "state_ref": admit_state(artifacts, env.scope, text, data_class),
                "question": {"question_id": "q", "text": "Is it?", **(question or {})},
                "label": label,
                "labelled_by": {"subject_id": "pinesky", "kind": "human"},
                "source": "operator",
            }
        )
    return write_label_set(env.store, env.scope, question_type=question_type, items=entries)


def qualify(
    env: Env, judge: Any, labels: Ref, *, repeats: int = 3, **thresholds: float
) -> dict[str, Any]:
    ref = JudgeQualifier(env.store, env.scope, artifacts=ArtifactStore(env.store)).qualify(
        judge, labels, repeats=repeats, thresholds=thresholds
    )
    value: dict[str, Any] = env.store.get(env.scope, "judge-qualification", ref)
    return value


def qualified(env: Env, judge: Any, question_type: str = "yes_no") -> dict[str, Any]:
    """Qualify a judge that answers `items()` exactly (a passing record)."""
    value = qualify(env, judge, label_set(env, items(), question_type))
    assert value["status"] == "pass", value
    return value


@pytest.fixture
def env(deployment: Any) -> Env:
    return Env(deployment)


@pytest.fixture
def service(env: Env, tmp_path: Path) -> JudgeService:
    root = tmp_path / "workspaces"
    root.mkdir()
    return JudgeService(env.store, env.scope, workspace_root=root)


# ======================================================================================
# 1. typed questions and answers (§6.6)
# ======================================================================================
def test_the_question_types_and_their_answer_shapes() -> None:
    assert YES.wire()["type"] == "yes_no" and "choices" not in YES.wire()
    assert CHOICE.wire()["choices"] == ["bug", "feature"]
    assert SCORE.wire()["scale"] == [0.0, 10.0]
    for q in (YES, CHOICE, SCORE):  # the text is stored as a digest, never the text
        wire = q.wire()
        assert wire["text_digest"].startswith("sha256:") and q.text not in json.dumps(wire)


@pytest.mark.parametrize(
    "bad",
    [
        {"question_id": ""},
        {"question_id": "has space"},
        {"text": "   "},
        {"text": "x" * 20_001},
        {"type": "essay"},
        {"type": "choice"},  # a choice needs choices
        {"type": "choice", "choices": ("only",)},
        {"type": "choice", "choices": ("a", "a")},
        {"type": "yes_no", "choices": ("a", "b")},  # choices only for a choice
        {"type": "score"},  # a score needs a scale
        {"type": "score", "scale": (5.0, 1.0)},
        {"type": "score", "scale": (0.0, float("inf"))},
        {"type": "yes_no", "scale": (0.0, 1.0)},
    ],
)
def test_a_malformed_question_is_a_fault(bad: dict[str, Any]) -> None:
    kwargs: dict[str, Any] = {"question_id": "q", "type": "yes_no", "text": "Is it?", **bad}
    fault("JUDGE_QUESTION", JudgeQuestion, **kwargs)


def test_check_answers_accepts_exactly_the_typed_answer_of_every_question() -> None:
    questions = [
        YES,
        JudgeQuestion("c", "choice", "k?", choices=("a", "b")),
        JudgeQuestion("s", "score", "n?", scale=(0.0, 1.0)),
    ]
    good = [
        JudgeAnswer("q", True, 0.7, True),
        JudgeAnswer("c", "b", 0.5, True),
        JudgeAnswer("s", 0.5, None, True),
    ]
    judges.check_answers(questions, good)
    bad: list[list[JudgeAnswer]] = [
        good[:2],  # a question left unanswered
        [*good, JudgeAnswer("q", True, 0.5, True)],  # an extra answer
        [JudgeAnswer("q", "yes", 0.7, True), *good[1:]],  # a string for a yes/no
        [JudgeAnswer("q", 1, 0.7, True), *good[1:]],  # an int is not a bool
        [good[0], JudgeAnswer("c", "z", 0.5, True), good[2]],  # not one of the choices
        [good[0], good[1], JudgeAnswer("s", 2.0, None, True)],  # outside the scale
        [good[0], good[1], JudgeAnswer("s", 0.5, 0.4, True)],  # a score has no probability
        [JudgeAnswer("q", True, 1.5, True), *good[1:]],  # probability above 1
        [JudgeAnswer("q", True, -0.1, True), *good[1:]],
        [JudgeAnswer("nope", True, 0.5, True), *good[1:]],  # an unknown question
        [JudgeAnswer("q", True, 0.7, True), JudgeAnswer("q", True, 0.7, True), good[2]],
    ]
    for answers in bad:
        hold("JUDGE_OUTPUT", judges.check_answers, questions, answers)


# ======================================================================================
# 2. connectors: NoJudge, LlmCellJudge over a read-only turn, JevJudge stub
# ======================================================================================
def test_no_judge_holds_judge_none() -> None:
    judge = NoJudge()
    assert judge.judge_id == "none" and judge.data_classes_allowed == frozenset()
    hold("JUDGE_NONE", judge.ask, STATE, [YES])


def test_an_llm_judge_asks_a_read_only_turn_and_returns_typed_answers() -> None:
    turn = JudgeTurn(reply={"answers": [answer("q", True, 0.8)]})
    judge = LlmCellJudge(turn)
    assert judge.judge_id == "llm_cell.judge-cell"
    assert "model-1" in judge.version and "high" in judge.version  # cell model and effort
    assert judge.data_classes_allowed == frozenset({"public", "internal"})  # §3.7 default
    answers = judge.ask(STATE, [YES])
    assert answers == [JudgeAnswer("q", True, 0.8, True)]  # probabilities are self-reported
    assert judge.last_usage == {"input_tokens": 30, "output_tokens": 12}
    (call,) = turn.calls
    prompt = call["prompt"]
    assert STATE.text in prompt and YES.text in prompt and "q (yes_no)" in prompt
    # the state is data in its own block, the instruction fixed (the planner pattern)
    assert prompt.index("JUDGE for AMPLAI") < prompt.index("<<<") < prompt.index(STATE.text)
    assert prompt.index(STATE.text) < prompt.index(">>>") < prompt.index(YES.text)
    schema = call["schema"]
    assert schema["required"] == ["answers"] and schema["additionalProperties"] is False
    item = schema["properties"]["answers"]["items"]
    assert item["additionalProperties"] is False
    assert item["properties"]["question_id"]["enum"] == ["q"]
    assert set(item["required"]) == {"question_id", "value", "probability"}


def test_an_llm_judge_answers_choice_and_score_questions() -> None:
    turn = JudgeTurn(reply={"answers": [answer("q", "feature", 0.6)]})
    judge = LlmCellJudge(turn)
    assert judge.ask(STATE, [CHOICE])[0].value == "feature"
    assert "Choices: bug, feature" in turn.calls[0]["prompt"]
    turn2 = JudgeTurn(reply={"answers": [answer("q", 7.5, None)]})
    score = LlmCellJudge(turn2).ask(STATE, [SCORE])[0]
    assert (score.value, score.probability) == (7.5, None)
    assert "Scale: 0.0 to 10.0" in turn2.calls[0]["prompt"]


def test_the_version_follows_the_cell_model_and_effort() -> None:
    base = LlmCellJudge(JudgeTurn()).version
    assert LlmCellJudge(JudgeTurn(model="model-2")).version != base
    assert LlmCellJudge(JudgeTurn(effort="low")).version != base
    assert LlmCellJudge(JudgeTurn(model=None, effort=None)).version != base
    assert LlmCellJudge(
        JudgeTurn(), data_classes_allowed=frozenset({"public"})
    ).data_classes_allowed == frozenset({"public"})


def test_an_llm_judge_without_a_cell_is_a_fault() -> None:
    fault("JUDGE_CONFIG", LlmCellJudge, JudgeTurn(cell_id=""))
    fault("JUDGE_CONFIG", LlmCellJudge, object())


def test_the_judge_turn_runs_on_an_empty_scratch_directory_that_is_removed(tmp_path: Path) -> None:
    """IC-14: no read-only turn runs on a copy of this repository."""
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    turn = JudgeTurn(reply={"answers": [answer("q", False, 0.2)]})
    LlmCellJudge(turn, scratch_root=scratch_root).ask(STATE, [YES])
    (call,) = turn.calls
    assert call["existed"] is True and call["entries"] == []  # an empty directory
    assert call["workspace"].parent == scratch_root  # under the given scratch root
    assert not call["workspace"].exists()  # and gone after the turn
    repo = Path(__file__).resolve().parents[2]
    assert repo not in call["workspace"].parents and call["workspace"] != repo


def test_the_judge_turn_reads_a_goals_workspace_without_copying_or_removing_it(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "goal-ws"
    workspace.mkdir()
    (workspace / "app.py").write_text("x = 1\n")
    turn = JudgeTurn(reply={"answers": [answer("q", True, 0.5)]})
    LlmCellJudge(turn).ask(JudgeState("t", "internal", workspace), [YES])
    (call,) = turn.calls
    assert call["workspace"] == workspace and call["entries"] == ["app.py"]
    assert workspace.is_dir() and (workspace / "app.py").read_text() == "x = 1\n"


@pytest.mark.parametrize(
    "reply",
    [{}, {"answers": "yes"}, {"answers": [None]}, {"answers": ["yes"]}, None],
)
def test_a_judge_reply_that_is_not_answers_holds_judge_output(reply: Any) -> None:
    judge = LlmCellJudge(JudgeTurn(reply=reply if reply is not None else {}))
    hold("JUDGE_OUTPUT", judge.ask, STATE, [YES])


def test_a_failing_turn_leaves_no_scratch_and_the_hold_reaches_the_caller(tmp_path: Path) -> None:
    class Failing(JudgeTurn):
        def run(self, **kwargs: Any) -> TurnResult:  # type: ignore[override]
            self.calls.append({"workspace": Path(kwargs["workspace"])})
            raise Hold("TURN_FAILED", "the provider refused")

    turn = Failing()
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    hold("TURN_FAILED", LlmCellJudge(turn, scratch_root=scratch_root).ask, STATE, [YES])
    assert list(scratch_root.iterdir()) == []


def test_the_jev_stub_holds_judge_not_configured_and_sends_nothing() -> None:
    for config in (None, JevConfig(), JevConfig(enabled=False, endpoint="https://example.invalid")):
        judge = JevJudge(config)
        assert judge.judge_id == "jev" and judge.version == "unmapped"
        assert judge.data_classes_allowed == frozenset({"public"})  # the §6.6 default
        hold("JUDGE_NOT_CONFIGURED", judge.ask, STATE, [YES])
    # enabled in the config still holds: the wire mapping is not written until the operator
    # confirms access (§14 Q9, plan.md §10.5), so nothing can be sent
    enabled = JevJudge(
        JevConfig(
            enabled=True,
            endpoint="https://example.invalid",
            token_file="/nonexistent",
            data_classes_allowed=("public", "internal"),
        )
    )
    assert enabled.data_classes_allowed == frozenset({"public", "internal"})
    held = hold("JUDGE_NOT_CONFIGURED", enabled.ask, STATE, [YES])
    assert "Q9" in held.message


# ======================================================================================
# 3. JudgeService.ask (§6.6, §2.6)
# ======================================================================================
def good_judge(**over: Any) -> Scripted:
    return Scripted(decide=lambda text, nth: (False, 0.1), **over)


def test_an_unqualified_judge_is_refused(env: Env, service: JudgeService) -> None:
    judge = good_judge()
    held = hold("JUDGE_UNQUALIFIED", service.ask, judge, STATE, [YES], subject={"goal_id": "g"})
    assert held.details == {"judge_id": "scripted.a", "version": "v1", "question_type": "yes_no"}
    assert judge.asked == {}  # never asked
    assert list(env.store.list_objects(env.scope, "judge-call")) == []


def test_qualification_is_per_judge_version_and_question_type(
    env: Env, service: JudgeService
) -> None:
    judge = Scripted(decide=exact)
    qualified(env, judge)
    service.ask(judge, STATE, [YES], subject={"goal_id": "g"})  # the qualified pair works
    other_version = Scripted(version="v2", decide=exact)  # a new judge version: new qualification
    hold("JUDGE_UNQUALIFIED", service.ask, other_version, STATE, [YES], subject={"goal_id": "g"})
    other_type = Scripted(decide=lambda t, n: ("bug", 0.5))
    hold("JUDGE_UNQUALIFIED", service.ask, other_type, STATE, [CHOICE], subject={"goal_id": "g"})
    other_judge = Scripted(judge_id="scripted.b", decide=exact)
    hold("JUDGE_UNQUALIFIED", service.ask, other_judge, STATE, [YES], subject={"goal_id": "g"})


def test_a_failed_qualification_does_not_qualify(env: Env, service: JudgeService) -> None:
    always_yes = Scripted(decide=lambda text, nth: (True, 0.9))  # right on half the items
    value = qualify(env, always_yes, label_set(env, items()))
    assert value["status"] == "fail"
    hold("JUDGE_UNQUALIFIED", service.ask, always_yes, STATE, [YES], subject={"goal_id": "g"})


def test_the_latest_qualification_of_a_pair_decides(env: Env, service: JudgeService) -> None:
    judge = Scripted(decide=exact)
    labels = label_set(env, items())
    assert qualify(env, judge, labels)["status"] == "pass"
    service.ask(judge, STATE, [YES], subject={"goal_id": "g"})
    # the same judge version now fails (it drifted): the newest record refuses it
    judge.decide = lambda text, nth: (not label_text(text), 0.9)
    assert qualify(env, judge, labels)["status"] == "fail"
    hold("JUDGE_UNQUALIFIED", service.ask, judge, STATE, [YES], subject={"goal_id": "g"})
    latest = current_qualification(env.store, env.scope, "scripted.a", "v1", "yes_no")
    assert latest is not None and latest["status"] == "fail"


def test_a_call_is_recorded_with_digests_only(env: Env, service: JudgeService) -> None:
    judge = Scripted(decide=exact)
    qualified(env, judge)
    state = JudgeState(text="a secret-looking goal text 12345", data_class="internal")
    question = JudgeQuestion("q", "yes_no", "Does the goal mention a password?")
    answers, ref = service.ask(judge, state, [question], subject={"goal_id": "goal-9"})
    assert [(a.question_id, a.self_reported) for a in answers] == [("q", True)]
    value = env.store.get(env.scope, "judge-call", ref)
    assert ref["id"].startswith("judgecall-")
    assert value["judge_id"] == "scripted.a" and value["judge_version"] == "v1"
    assert value["question_type"] == "yes_no" and value["data_class"] == "internal"
    assert value["subject"] == {"goal_id": "goal-9"}
    assert value["answers"] == [
        {"question_id": "q", "value": False, "probability": 0.05, "self_reported": True}
    ]
    assert value["usage"] == {"input_tokens": 50, "output_tokens": 0}
    assert isinstance(value["latency_ms"], int) and value["latency_ms"] >= 0 and value["at"]
    assert value["state_digest"].startswith("sha256:")
    assert value["questions"][0]["text_digest"].startswith("sha256:")
    stored = json.dumps(value)
    assert state.text not in stored and question.text not in stored  # texts only as digests


def test_a_data_class_the_judge_does_not_allow_is_refused(env: Env, service: JudgeService) -> None:
    judge = Scripted(decide=exact)
    qualified(env, judge)
    for data_class in ("confidential", "restricted"):
        held = hold(
            "JUDGE_DATA_CLASS",
            service.ask,
            judge,
            JudgeState("t", data_class),
            [YES],  # type: ignore[arg-type]
            subject={"goal_id": "g"},
        )
        assert held.details["allowed"] == ["internal", "public"]
    hold(
        "JUDGE_DATA_CLASS",
        service.ask,
        judge,
        JudgeState("t", "secret"),
        [YES],  # type: ignore[arg-type]
        subject={"goal_id": "g"},
    )
    assert judge.asked.get("t") is None  # refused before the judge saw the text


def test_an_answer_of_the_wrong_type_holds_judge_output_and_writes_no_call(
    env: Env, service: JudgeService
) -> None:
    judge = Scripted(decide=exact)
    qualified(env, judge)
    judge.decide = lambda text, nth: ("yes", 0.5)  # a string for a yes/no question
    hold("JUDGE_OUTPUT", service.ask, judge, STATE, [YES], subject={"goal_id": "g"})
    assert list(env.store.list_objects(env.scope, "judge-call")) == []


def test_a_call_asks_one_type_with_distinct_question_ids(env: Env, service: JudgeService) -> None:
    judge = Scripted(decide=exact)
    qualified(env, judge)
    kwargs: dict[str, Any] = {"subject": {"goal_id": "g"}}
    fault("JUDGE_QUESTION", service.ask, judge, STATE, [], **kwargs)
    fault("JUDGE_QUESTION", service.ask, judge, STATE, [YES, CHOICE], **kwargs)
    fault("JUDGE_QUESTION", service.ask, judge, STATE, [YES, YES], **kwargs)


# -- IC-14: JUDGE_WORKSPACE --------------------------------------------------------------------
def test_a_workspace_outside_the_workspace_root_holds_judge_workspace(
    env: Env, service: JudgeService, tmp_path: Path
) -> None:
    judge = Scripted(decide=exact)
    qualified(env, judge)
    root = service.workspace_root
    assert root is not None
    inside = root / "goal-1"
    inside.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    repo = Path(__file__).resolve().parents[2]  # a copy of this repository is never a workspace
    service.ask(judge, JudgeState("t", "internal", inside), [YES], subject={"goal_id": "g"})
    kwargs: dict[str, Any] = {"subject": {"goal_id": "g"}}
    for bad in (outside, repo, root, root / "missing", root.parent):
        held = hold(
            "JUDGE_WORKSPACE", service.ask, judge, JudgeState("t", "internal", bad), [YES], **kwargs
        )
        assert str(bad) in held.details["workspace"]
    # a symlink under the root that points out of it is outside
    link = root / "linked"
    link.symlink_to(outside, target_is_directory=True)
    hold("JUDGE_WORKSPACE", service.ask, judge, JudgeState("t", "internal", link), [YES], **kwargs)
    # a file is not a workspace
    (root / "file.txt").write_text("x")
    hold(
        "JUDGE_WORKSPACE",
        service.ask,
        judge,
        JudgeState("t", "internal", root / "file.txt"),
        [YES],
        **kwargs,
    )


def test_without_a_workspace_root_no_workspace_is_accepted(env: Env, tmp_path: Path) -> None:
    judge = Scripted(decide=exact)
    qualified(env, judge)
    bare = JudgeService(env.store, env.scope)
    bare.ask(judge, STATE, [YES], subject={"goal_id": "g"})  # no workspace: fine
    some = tmp_path / "ws"
    some.mkdir()
    hold(
        "JUDGE_WORKSPACE",
        bare.ask,
        judge,
        JudgeState("t", "internal", some),
        [YES],
        subject={"goal_id": "g"},
    )


def test_the_workspace_check_runs_before_the_judge_is_asked(
    env: Env, service: JudgeService, tmp_path: Path
) -> None:
    turn = JudgeTurn(reply={"answers": [answer("q", True, 0.9)]})
    llm = LlmCellJudge(turn)
    qualified_llm(env, llm)
    hold(
        "JUDGE_WORKSPACE",
        service.ask,
        llm,
        JudgeState("t", "internal", tmp_path),
        [YES],
        subject={"goal_id": "g"},
    )
    assert turn.calls == []  # no read-only turn started on an outside directory


def qualified_llm(env: Env, judge: LlmCellJudge) -> None:
    """Qualify an LLM judge whose scripted turn answers by the state text."""
    turn = judge.turn

    def reply(prompt: str) -> dict[str, Any]:
        value = prompt.split("<<<")[1].split(">>>")[0].strip().endswith("Y")
        return {"answers": [answer("q", value, 0.95 if value else 0.05)]}

    previous, turn.reply = turn.reply, reply
    try:
        qualified(env, judge)
    finally:
        turn.calls.clear()
        turn.reply = previous


# ======================================================================================
# 4. qualification (§6.7)
# ======================================================================================
def test_a_qualification_records_accuracy_calibration_stability_and_cost(env: Env) -> None:
    judge = Scripted(decide=exact, tokens=70)
    value = qualify(env, judge, label_set(env, items(40)))
    ref = next(r for r, v in qualifications(env.store, env.scope, "scripted.a") if v == value)
    assert ref["id"] == "judgequal-scripted.a-yes_no-1"
    assert value["schema"] == "amplai.judge-qualification.v1"
    assert (value["judge_id"], value["judge_version"], value["question_type"]) == (
        "scripted.a",
        "v1",
        "yes_no",
    )
    assert value["n"] == 40 and value["accuracy"] == 1.0 and value["repeats"] == 3
    # Wilson 95 % interval for 40 / 40, recomputed here
    from test_033_s10_deciders import wilson_ref

    low, high = wilson_ref(40, 40)
    assert value["accuracy_interval"] == pytest.approx([low, min(high, 1.0)], abs=1e-5)
    # Brier: every probability is 0.05 from the outcome; ECE over 10 bins: the 0.95 bin is right
    # 100 % of the time and the 0.05 bin 0 % of the time, so each bin misses by 0.05
    assert value["brier"] == pytest.approx(0.0025, abs=1e-6)
    assert value["ece_10"] == pytest.approx(0.05, abs=1e-6)
    assert value["repeat_agreement"] == 1.0
    assert value["median_cost_tokens"] == 70 and value["median_latency_ms"] >= 0
    assert (
        value["thresholds"]
        == DEFAULT_THRESHOLDS
        == {
            "accuracy_lower": 0.8,
            "repeat_agreement": 0.9,
            "ece_10": 0.1,
        }
    )
    assert value["status"] == "pass" and value["qualified_at"]
    assert value["label_set_ref"]["id"].startswith("labels-yes_no-")
    assert judge.asked["case 0 Y"] == 3  # every item asked `repeats` times


def test_qualification_records_are_numbered_per_pair_and_never_overwritten(env: Env) -> None:
    judge = Scripted(decide=exact)
    labels = label_set(env, items(40))
    first = qualify(env, judge, labels)
    second = qualify(env, judge, labels)
    ids = [r["id"] for r, _v in qualifications(env.store, env.scope, "scripted.a")]
    assert ids == ["judgequal-scripted.a-yes_no-1", "judgequal-scripted.a-yes_no-2"]
    assert first["judge_version"] == second["judge_version"]
    new_version = Scripted(version="v2", decide=exact)
    qualify(env, new_version, labels)
    ids = [r["id"] for r, _v in qualifications(env.store, env.scope, "scripted.a")]
    assert ids[-1] == "judgequal-scripted.a-yes_no-3"  # a new version needs a new qualification


def test_accuracy_below_the_threshold_fails(env: Env) -> None:
    # right on 30 of 40: 0.75, Wilson lower bound ~0.6 < 0.8
    def mostly(text: str, nth: int) -> tuple[Any, float | None]:
        index = int(text.split()[1])
        value = label_text(text) if index >= 10 else not label_text(text)
        return value, 0.9 if value else 0.1

    value = qualify(env, Scripted(decide=mostly), label_set(env, items(40)))
    assert value["accuracy"] == 0.75 and value["accuracy_interval"][0] < 0.8
    assert value["status"] == "fail"
    # thresholds are the caller's to set: with a lower bound of 0.5 the same judge passes
    relaxed = qualify(
        env, Scripted(decide=mostly), label_set(env, items(40)), accuracy_lower=0.5, ece_10=0.5
    )
    assert relaxed["status"] == "pass" and relaxed["thresholds"]["accuracy_lower"] == 0.5


def test_a_small_label_set_cannot_reach_the_accuracy_bound(env: Env) -> None:
    # 8 of 8 right still has a Wilson lower bound below 0.8 (~0.68): too few labels to qualify
    value = qualify(env, Scripted(decide=exact), label_set(env, items(8)))
    assert value["accuracy"] == 1.0 and value["accuracy_interval"][0] < 0.8
    assert value["status"] == "fail"


def test_an_unstable_judge_fails_on_repeat_agreement(env: Env) -> None:
    def flips(text: str, nth: int) -> tuple[Any, float | None]:
        value = label_text(text) if nth == 1 else (nth == 2)  # right once, then changes its mind
        return value, 0.9 if value else 0.1

    value = qualify(env, Scripted(decide=flips), label_set(env, items(40)))
    assert value["accuracy"] == 1.0  # accuracy is the first ask of each item
    assert value["repeat_agreement"] < 0.9 and value["status"] == "fail"


def test_a_miscalibrated_judge_fails_on_ece_even_when_accurate_and_stable(env: Env) -> None:
    def timid(text: str, nth: int) -> tuple[Any, float | None]:
        return label_text(text), 0.6 if label_text(text) else 0.4  # right, but unsure

    value = qualify(env, Scripted(decide=timid), label_set(env, items(40)))
    assert value["accuracy"] == 1.0 and value["repeat_agreement"] == 1.0
    assert value["ece_10"] == pytest.approx(0.4, abs=1e-6) and value["status"] == "fail"


def test_a_judge_that_gives_no_probability_is_not_judged_on_calibration(env: Env) -> None:
    value = qualify(
        env, Scripted(decide=lambda t, n: (label_text(t), None)), label_set(env, items(40))
    )
    assert value["brier"] is None and value["ece_10"] is None and value["status"] == "pass"


def test_choice_and_score_questions_are_qualified_on_accuracy_and_stability(env: Env) -> None:
    kinds = [
        (f"c{i} {'bug' if i % 2 else 'feature'}", "bug" if i % 2 else "feature") for i in range(40)
    ]
    judge = Scripted(decide=lambda t, n: (t.split()[1], 0.5))
    choice = {"choices": ["bug", "feature"]}
    value = qualify(env, judge, label_set(env, kinds, "choice", question=choice))
    assert value["question_type"] == "choice" and value["accuracy"] == 1.0
    assert value["ece_10"] is None and value["brier"] is None  # calibration is yes/no only
    assert value["status"] == "pass"
    scores = [(f"s{i}", float(i % 5)) for i in range(40)]
    scorer = Scripted(judge_id="scripted.s", decide=lambda t, n: (float(int(t[1:]) % 5), None))
    scored = qualify(env, scorer, label_set(env, scores, "score", question={"scale": [0.0, 10.0]}))
    assert scored["question_type"] == "score" and scored["accuracy"] == 1.0
    assert scored["status"] == "pass"


def test_a_state_of_a_class_the_judge_does_not_allow_fails_the_qualification(env: Env) -> None:
    judge = Scripted(decide=exact)
    value = qualify(env, judge, label_set(env, items(40), data_class="confidential"))
    assert value["status"] == "fail" and value["n"] == 0
    assert "JUDGE_DATA_CLASS" in value["errors"] and judge.asked == {}


def test_a_judge_that_holds_on_every_ask_fails_with_its_code(env: Env) -> None:
    labels = label_set(env, items(10), data_class="public")  # Jev allows public states only
    value = qualify(env, JevJudge(JevConfig(enabled=True)), labels)
    assert value["status"] == "fail" and value["n"] == 0
    assert value["errors"] == ["JUDGE_NOT_CONFIGURED"]
    assert value["accuracy"] is None and value["median_cost_tokens"] is None


def test_qualification_arguments_are_checked(env: Env) -> None:
    judge = Scripted(decide=exact)
    labels = label_set(env, items(10))
    qualifier = JudgeQualifier(env.store, env.scope)
    for repeats in (0, 11, 2.5):
        fault(
            "JUDGE_QUALIFICATION",
            qualifier.qualify,
            judge,
            labels,
            repeats=repeats,  # type: ignore[arg-type]
            thresholds={},
        )
    fault("JUDGE_QUALIFICATION", qualifier.qualify, judge, labels, thresholds={"speed": 0.5})
    fault(
        "JUDGE_QUALIFICATION", qualifier.qualify, judge, labels, thresholds={"accuracy_lower": 1.5}
    )
    assert judge.asked == {}


def test_a_label_set_is_validated_and_content_addressed(env: Env) -> None:
    artifacts = ArtifactStore(env.store)
    entries = [
        {
            "item_id": f"i{i}",
            "state_ref": admit_state(artifacts, env.scope, text, "internal"),
            "question": {"question_id": "q", "text": "Is it?"},
            "label": label,
            "labelled_by": {"subject_id": "pinesky", "kind": "human"},
            "source": "operator",
        }
        for i, (text, label) in enumerate(items(10))
    ]
    first = write_label_set(env.store, env.scope, question_type="yes_no", items=entries)
    again = write_label_set(env.store, env.scope, question_type="yes_no", items=entries)
    assert first == again and first["id"].startswith("labels-yes_no-")  # id from the items
    assert len(first["id"]) == len("labels-yes_no-") + 24
    changed = write_label_set(env.store, env.scope, question_type="yes_no", items=entries[:-1])
    assert changed["id"] != first["id"]
    fault("JUDGE_LABELS", write_label_set, env.store, env.scope, question_type="yes_no", items=[])
    fault(
        "JUDGE_LABELS", write_label_set, env.store, env.scope, question_type="essay", items=entries
    )
    bad_source = [{**entries[0], "source": "guess"}]
    fault(
        "JUDGE_LABELS",
        write_label_set,
        env.store,
        env.scope,
        question_type="yes_no",
        items=bad_source,
    )
    fault("JUDGE_LABELS", admit_state, artifacts, env.scope, "", "internal")
    fault("JUDGE_LABELS", admit_state, artifacts, env.scope, "t", "secret")


def test_a_label_item_whose_state_is_not_an_artifact_cannot_qualify(env: Env) -> None:
    entries = [
        {
            "item_id": "i0",
            "state_ref": {"id": "x", "revision": 1, "digest": "sha256:" + "0" * 64},
            "question": {"question_id": "q", "text": "Is it?"},
            "label": True,
            "labelled_by": {"subject_id": "pinesky"},
            "source": "operator",
        }
    ]
    labels = write_label_set(env.store, env.scope, question_type="yes_no", items=entries)
    fault(
        "JUDGE_LABELS",
        JudgeQualifier(env.store, env.scope).qualify,
        Scripted(decide=exact),
        labels,
        thresholds={},
    )


# ======================================================================================
# 5. the Jev stub through the service and qualification
# ======================================================================================
def test_jev_holds_judge_not_configured_even_when_a_qualification_record_exists(
    env: Env, service: JudgeService
) -> None:
    """The qualification of `jev` can only fail (its `ask` holds). Planting a passing record
    shows the connector's own hold is what stops the call: nothing reaches a network."""
    jev = JevJudge(JevConfig(enabled=True, endpoint="https://example.invalid"))
    assert qualify(env, jev, label_set(env, items(10), data_class="public"))["status"] == "fail"
    hold("JUDGE_UNQUALIFIED", service.ask, jev, STATE, [YES], subject={"goal_id": "g"})
    planted = {
        "schema": "amplai.judge-qualification.v1",
        "scope": env.scope.wire(),
        "judge_id": "jev",
        "judge_version": "unmapped",
        "question_type": "yes_no",
        "label_set_ref": label_set(env, items(10)),
        "n": 10,
        "accuracy": 1.0,
        "accuracy_interval": [0.9, 1.0],
        "brier": None,
        "ece_10": None,
        "repeat_agreement": 1.0,
        "repeats": 3,
        "median_cost_tokens": None,
        "median_latency_ms": None,
        "thresholds": dict(DEFAULT_THRESHOLDS),
        "status": "pass",
        "qualified_at": "2026-10-01T00:00:00Z",
    }
    env.put("judge-qualification", "judgequal-jev-yes_no-99", planted)
    held = hold(
        "JUDGE_NOT_CONFIGURED",
        service.ask,
        jev,
        JudgeState("t", "public"),
        [YES],
        subject={"goal_id": "g"},
    )
    assert held.code == "JUDGE_NOT_CONFIGURED"
    assert list(env.store.list_objects(env.scope, "judge-call")) == []


# ======================================================================================
# 6. choosing a judge (§6.6 last rule)
# ======================================================================================
def accurate(correct: int, n: int = 40) -> Callable[[str, int], tuple[Any, float | None]]:
    """A judge right on the first `correct` of `n` items."""

    def decide(text: str, nth: int) -> tuple[Any, float | None]:
        index = int(text.split()[1])
        value = label_text(text) if index < correct else not label_text(text)
        return value, 0.95 if value else 0.05

    return decide


def test_the_chosen_judge_is_not_credibly_less_accurate_then_cheapest_then_fastest(
    env: Env,
) -> None:
    labels = label_set(env, items(100))
    best = Scripted(judge_id="llm_cell.best", decide=accurate(97, 100), tokens=500)
    cheap = Scripted(judge_id="llm_cell.cheap", decide=accurate(94, 100), tokens=100)
    cheaper_but_worse = Scripted(judge_id="llm_cell.worse", decide=accurate(80, 100), tokens=10)
    unqualified = Scripted(judge_id="llm_cell.new", decide=accurate(100, 100), tokens=1)
    for judge in (best, cheap, cheaper_but_worse):
        qualify(env, judge, labels, accuracy_lower=0.0, ece_10=1.0)
    everyone = [best, cheap, cheaper_but_worse, unqualified]
    assert qualified_chooser(env.store, env.scope, everyone)("yes_no") is cheap
    # the one that is credibly less accurate (upper bound 0.87 < 0.97) is never chosen however cheap
    assert choose_qualified(env.store, env.scope, [best, cheaper_but_worse], "yes_no") is best
    assert choose_qualified(env.store, env.scope, [unqualified], "yes_no") is None  # unqualified
    assert choose_qualified(env.store, env.scope, everyone, "choice") is None  # other type
    assert choose_qualified(env.store, env.scope, [], "yes_no") is None
    # equal accuracy and cost: the faster one (median latency) wins; here only ids differ
    twin = Scripted(judge_id="llm_cell.twin", decide=accurate(94, 100), tokens=100)
    qualify(env, twin, labels, accuracy_lower=0.0, ece_10=1.0)
    chosen = choose_qualified(env.store, env.scope, [cheap, twin], "yes_no")
    assert chosen in (cheap, twin)


def test_the_decider_judge_model_limits_which_judge_may_be_chosen(env: Env) -> None:
    labels = label_set(env, items(100))
    a = Scripted(judge_id="llm_cell.a", decide=accurate(99, 100), tokens=500)
    b = Scripted(judge_id="llm_cell.b", decide=accurate(99, 100), tokens=100)
    for judge in (a, b):
        qualify(env, judge, labels, accuracy_lower=0.0, ece_10=1.0)
    both = [a, b]
    allow_a = {"judge": "llm_cell", "cell": "a", "question_types": ["yes_no"]}
    assert choose_qualified(env.store, env.scope, both, "yes_no", allowed=allow_a) is a
    assert choose_qualified(env.store, env.scope, both, "choice", allowed=allow_a) is None
    assert choose_qualified(env.store, env.scope, both, "yes_no", allowed={"judge": "none"}) is None
    jev_only = {"judge": "jev", "question_types": ["yes_no"]}
    assert choose_qualified(env.store, env.scope, both, "yes_no", allowed=jev_only) is None
    assert choose_qualified(env.store, env.scope, both, "yes_no") is b  # unrestricted: cheapest


def test_the_judge_model_component_is_validated(env: Env) -> None:
    env.register("judge_model", "none", {"judge": "none"})
    env.register(
        "judge_model",
        "cell",
        {"judge": "llm_cell", "cell": "codex-cli", "question_types": ["yes_no", "choice"]},
    )
    env.register("judge_model", "jev", {"judge": "jev", "question_types": ["yes_no"]})
    assert policies.V1["judge_model"] == {"judge": "none"}
    bad = [
        {"judge": "oracle"},
        {"judge": "llm_cell", "question_types": ["yes_no"]},  # an LLM judge names its cell
        {"judge": "llm_cell", "cell": "c"},  # and a question type
        {"judge": "jev", "cell": "c", "question_types": ["yes_no"]},  # Jev has no cell
        {"judge": "none", "cell": "c"},
        {"judge": "none", "question_types": ["yes_no"]},
        {"judge": "llm_cell", "cell": "c", "question_types": ["essay"]},
        {"judge": "llm_cell", "cell": "c", "question_types": ["yes_no", "yes_no"]},
    ]
    for i, content in enumerate(bad):
        fault("COMPONENT_CONTENT", env.register, "judge_model", f"bad{i}", content)


# ======================================================================================
# 7. a judge answers the L1 question of a decider (§6.2 `judge_v1`, IC-21)
# ======================================================================================
def l1_judge_decider(env: Env, *, estimator: str = "judge_v1", fallback: str = "prior_v1") -> Ref:
    method = env.method(["questions"], estimator=estimator, fallback=fallback)
    judge = env.register(
        "judge_model",
        "l1",
        {"judge": "llm_cell", "cell": "judge-cell", "question_types": ["yes_no"]},
    )
    return env.decider("L1", method, None, judge=judge)


def ctx(options: tuple[str, ...] = ("proceed", "ask_back"), **over: Any) -> DecisionContext:
    base: dict[str, Any] = {
        "layer": "L1",
        "cell_id": CELL,
        "features": {"questions": 1},
        "options": options,
        "prior": "ask_back",
        "subject": {"goal_id": "g-judge"},
        "production": True,
        "judge_state": JudgeState("make value return 2", "internal"),
    }
    base.update(over)
    return DecisionContext(**base)


def judged(
    env: Env, service: JudgeService, ref: Ref, *, says: bool, cap: int = 1_000
) -> tuple[Any, AuxLedger, JudgeTurn]:
    turn = JudgeTurn(cell_id="judge-cell", reply={"answers": [answer("ambiguous", says, 0.9)]})
    judge = LlmCellJudge(turn)
    qualified_llm_ambiguity(env, judge)
    ledger = AuxLedger(cap)
    decider = Decider.of(
        env.store,
        env.scope,
        env.content(ref),
        ref=ref,
        judges=service,
        choose_judge=qualified_chooser(
            env.store,
            env.scope,
            [judge],
            allowed=env.store.get(env.scope, "harness-component", env.content(ref)["judge"])[
                "content"
            ],
        ),
        aux=ledger,
    )
    turn.reply = {"answers": [answer("ambiguous", says, 0.9)]}
    return decider, ledger, turn


def qualified_llm_ambiguity(env: Env, judge: LlmCellJudge) -> None:
    turn = judge.turn
    saved = turn.reply

    def reply(prompt: str) -> dict[str, Any]:
        said = prompt.split("<<<")[1].split(">>>")[0].strip().endswith("Y")
        return {
            "answers": [{"question_id": "q", "value": said, "probability": 0.95 if said else 0.05}]
        }

    turn.reply = reply
    try:
        qualified(env, judge)
    finally:
        turn.reply = saved
        turn.calls.clear()


def test_a_qualified_judge_answers_yes_so_the_l1_decider_asks_back(
    env: Env, service: JudgeService
) -> None:
    ref = l1_judge_decider(env)
    decider, ledger, turn = judged(env, service, ref, says=True)
    decision = decider.decide(ctx())
    assert decision.option == "ask_back" and decision.used_prior is False
    value = env.store.get(env.scope, "harness-decision", decision.record_ref)
    assert value["judge_call_ref"] is not None and value["table_ref"] is None
    call = env.store.get(env.scope, "judge-call", value["judge_call_ref"])
    assert call["subject"] == {"goal_id": "g-judge"} and call["question_type"] == "yes_no"
    assert call["questions"][0]["question_id"] == "ambiguous"
    assert AMBIGUITY_QUESTION in turn.calls[-1]["prompt"]  # the plan.md §10.2 question
    # a judge turn is auxiliary (IC-21): the ledger holds it with its tokens
    (entry,) = ledger.entries
    assert (entry["role"], entry["purpose"], entry["tokens"]) == ("judge", "L1", 42)
    assert entry["error"] is None and entry["cell_id"] == "judge-cell"


def test_a_qualified_judge_answering_no_lets_the_goal_proceed(
    env: Env, service: JudgeService
) -> None:
    ref = l1_judge_decider(env)
    decider, _ledger, _turn = judged(env, service, ref, says=False)
    decision = decider.decide(ctx(prior="ask_back"))
    assert decision.option == "proceed" and decision.used_prior is False


def test_a_judge_answer_for_an_option_that_cannot_run_falls_back_to_the_prior(
    env: Env, service: JudgeService
) -> None:
    ref = l1_judge_decider(env)
    decider, _ledger, _turn = judged(env, service, ref, says=True)  # yes -> ask_back
    decision = decider.decide(
        ctx(ineligible={"ask_back": "the planner asked nothing"}, prior="proceed")
    )
    assert decision.option == "proceed" and decision.used_prior is True


def test_an_unqualified_judge_is_never_asked_by_a_decider(env: Env, service: JudgeService) -> None:
    ref = l1_judge_decider(env)
    turn = JudgeTurn(cell_id="judge-cell", reply={"answers": [answer("ambiguous", True)]})
    ledger = AuxLedger(1_000)
    decider = Decider.of(
        env.store,
        env.scope,
        env.content(ref),
        ref=ref,
        judges=service,
        choose_judge=qualified_chooser(env.store, env.scope, [LlmCellJudge(turn)]),
        aux=ledger,
    )
    decision = decider.decide(ctx())
    assert decision.used_prior is True and decision.option == "ask_back"
    assert turn.calls == [] and ledger.entries == []
    assert list(env.store.list_objects(env.scope, "judge-call")) == []


def test_the_auxiliary_cap_stops_a_judge_turn_before_it_starts(
    env: Env, service: JudgeService
) -> None:
    ref = l1_judge_decider(env)
    decider, ledger, turn = judged(env, service, ref, says=True, cap=0)  # v1: no aux budget
    decision = decider.decide(ctx())
    assert decision.used_prior is True  # real goals take the prior when no aux tokens are set aside
    assert [c for c in turn.calls] == [] and ledger.entries == []
    why = [
        o["why"]
        for o in env.store.get(env.scope, "harness-decision", decision.record_ref)["options"]
        if o["option"] == "ask_back"
    ]
    assert why and "prior" in why[0]


def test_a_judge_estimator_outside_l1_and_a_decider_without_a_ledger_take_the_prior(
    env: Env, service: JudgeService
) -> None:
    ref = l1_judge_decider(env)
    decider, _ledger, turn = judged(env, service, ref, says=True)
    outside = decider.decide(
        ctx(layer="L2", options=("repair_loop", "single"), prior="repair_loop", features={})
    )
    assert outside.used_prior is True and turn.calls == []  # no question is defined at L2
    no_ledger = Decider.of(
        env.store,
        env.scope,
        env.content(ref),
        ref=ref,
        judges=service,
        choose_judge=lambda t: LlmCellJudge(turn),
    )
    assert no_ledger.decide(ctx()).used_prior is True and turn.calls == []


def test_the_judge_fallback_asks_only_after_the_table_has_nothing_to_say(
    env: Env, service: JudgeService
) -> None:
    ref = l1_judge_decider(env, estimator="pooled_beta_binomial_v1", fallback="judge_v1")
    decider, ledger, _turn = judged(env, service, ref, says=False)
    decision = decider.decide(ctx())  # no table: the fallback judge answers
    assert (
        decision.option == "proceed" and decision.used_prior is False and len(ledger.entries) == 1
    )
    assert env.store.get(env.scope, "harness-decision", decision.record_ref)["judge_call_ref"]


# ======================================================================================
# 8. a goal's L1 decision with a judge, in the real plan (§6.1, §6.6, IC-14, IC-21)
# ======================================================================================
class JudgeTurns:
    """The `turns` factory of the strategy runner: the judge cell's scripted turn."""

    def __init__(self, turn: JudgeTurn) -> None:
        self.turn = turn

    def __call__(self, cell_id: str) -> JudgeTurn:
        return self.turn


def judge_world(
    deployment: Any, tmp_path: Path, *, says: bool, aux: int
) -> tuple[Any, Ref, JudgeTurn, Env]:
    w = build_world(deployment, tmp_path, draft=QUESTION_DRAFT)
    e = env_of(w)
    turn = JudgeTurn(cell_id=CELL, reply={"answers": [answer("ambiguous", says, 0.9)]})
    w.runner.turns = JudgeTurns(turn)  # type: ignore[assignment]
    method = e.method(["questions"], estimator="judge_v1")
    judge = e.register(
        "judge_model", "l1real", {"judge": "llm_cell", "cell": CELL, "question_types": ["yes_no"]}
    )
    decider = e.decider("L1", method, None, judge=judge)
    # the judge of the plan is the connector of the installed cell (same id, model, effort)
    connector = next(iter(_connector_of(w, judge)))
    qualified_llm_ambiguity(e, connector)
    limits = e.register("limits", "aux", {**policies.V1["limits"], "aux_max_tokens": aux})
    composition = candidate(w, L1=decider, limits=limits)
    return w, composition, turn, e


def _connector_of(w: Any, judge_ref: Ref) -> list[LlmCellJudge]:
    turn = w.runner.turns(CELL)
    return [
        LlmCellJudge(
            turn,
            data_classes_allowed=frozenset({"public", "internal"}),
            scratch_root=w.rig.workspaces.root,
        )
    ]


def test_the_plan_asks_a_qualified_judge_at_l1_and_records_the_call_and_its_tokens(
    deployment: Any, tmp_path: Path
) -> None:
    w, composition, turn, e = judge_world(deployment, tmp_path, says=False, aux=1_000_000)
    turn.calls.clear()
    goal = w.plan(composition)
    record = w.record(goal)
    assert record["status"] == "awaiting_approval"  # "no": proceed despite the planner's question
    l1 = only(w, goal, "L1")
    assert l1["chosen"] == "proceed" and l1["used_prior"] is False
    call = e.store.get(e.scope, "judge-call", l1["judge_call_ref"])
    assert call["judge_id"] == "llm_cell." + CELL and call["subject"] == {"goal_id": goal}
    assert [entry["role"] for entry in record["aux_usage"]] == ["judge"]
    assert record["aux_usage"][0]["purpose"] == "L1" and record["aux_usage"][0]["tokens"] == 42
    (turn_call,) = turn.calls
    # IC-14: an empty scratch directory under the workspace manager's root, removed after
    assert turn_call["entries"] == [] and not turn_call["workspace"].exists()
    assert w.rig.workspaces.root in turn_call["workspace"].parents
    assert layers_of(w, goal) == ["L1", "L2", "L3", "L8"]


def test_the_plan_asks_back_when_the_judge_says_the_goal_is_ambiguous(
    deployment: Any, tmp_path: Path
) -> None:
    w, composition, _turn, _e = judge_world(deployment, tmp_path, says=True, aux=1_000_000)
    goal = w.plan(composition)
    assert w.record(goal)["status"] == "needs_answers"
    assert only(w, goal, "L1")["chosen"] == "ask_back" and layers_of(w, goal) == ["L1"]


def test_a_real_goal_with_no_auxiliary_budget_never_asks_a_judge(
    deployment: Any, tmp_path: Path
) -> None:
    """IC-21: aux_max_tokens is 0 in v1, so a judge turn cannot start for a real goal."""
    w, composition, turn, _e = judge_world(deployment, tmp_path, says=False, aux=0)
    turn.calls.clear()
    goal = w.plan(composition)
    assert turn.calls == []
    l1 = only(w, goal, "L1")
    assert l1["used_prior"] is True and l1["chosen"] == "ask_back" and l1["judge_call_ref"] is None
    assert w.record(goal)["status"] == "needs_answers"  # today's branch
    assert not any(d["layer"] == "L1" and d["judge_call_ref"] for d in decisions_of(w, goal))


def test_an_unqualified_judge_cell_is_never_asked_in_a_plan(
    deployment: Any, tmp_path: Path
) -> None:
    w = build_world(deployment, tmp_path, draft=QUESTION_DRAFT)
    e = env_of(w)
    turn = JudgeTurn(cell_id=CELL, reply={"answers": [answer("ambiguous", False, 0.9)]})
    w.runner.turns = JudgeTurns(turn)  # type: ignore[assignment]
    method = e.method(["questions"], estimator="judge_v1")
    judge = e.register(
        "judge_model", "l1un", {"judge": "llm_cell", "cell": CELL, "question_types": ["yes_no"]}
    )
    decider = e.decider("L1", method, None, judge=judge)
    limits = e.register("limits", "aux", {**policies.V1["limits"], "aux_max_tokens": 1_000_000})
    goal = w.plan(candidate(w, L1=decider, limits=limits))
    assert turn.calls == [] and only(w, goal, "L1")["used_prior"] is True
    assert w.record(goal)["status"] == "needs_answers"


def test_a_jev_judge_in_a_decider_is_refused_before_the_planner_turn(
    deployment: Any, tmp_path: Path
) -> None:
    w = build_world(deployment, tmp_path, draft=QUESTION_DRAFT)
    e = env_of(w)
    method = e.method(["questions"], estimator="judge_v1")
    judge = e.register("judge_model", "jev", {"judge": "jev", "question_types": ["yes_no"]})
    decider = e.decider("L1", method, None, judge=judge)
    composition = candidate(w, L1=decider)
    held = fault("COMPONENT_CONTENT", w.service.plan, _goal(w), composition=composition)
    assert any("Jev" in str(part) for part in held.details)  # §14 Q9: nothing is configured
    assert w.rig.planner.calls == []  # refused before the planner turn


def _goal(w: Any) -> str:
    from rc06_rig import submit

    return str(submit(w.rig, "ask jev"))


# ======================================================================================
# 9. the `amplai meta judge` command functions (§12.1)
# ======================================================================================
def command_dep(w: Any, **config: Any) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        store=w.rig.d.store,
        scope=w.rig.d.scope,
        service=w.service,
        artifacts=w.rig.d.artifacts,
        config=SimpleNamespace(**config),
        operator=lambda: w.rig.operator,
    )


def test_the_connector_of_a_judge_id(deployment: Any, tmp_path: Path) -> None:
    from types import SimpleNamespace

    from amplai_foundry.runtime.meta_commands import judges as commands

    w = build_world(deployment, tmp_path)
    w.runner.turns = JudgeTurns(JudgeTurn(cell_id=CELL))  # type: ignore[assignment]
    dep = command_dep(w)
    llm = commands.connector(dep, "llm_cell." + CELL)
    assert isinstance(llm, LlmCellJudge) and llm.judge_id == "llm_cell." + CELL
    assert llm.scratch_root == w.rig.workspaces.root  # IC-14: scratch under the workspace root
    composition = w.rig.d.store.get(
        w.rig.d.scope, "harness-composition", w.service.apps["app"].compositions[CELL]
    )
    model = w.rig.d.store.get(w.rig.d.scope, "model-profile", composition["model_profile_ref"])
    assert llm.data_classes_allowed == frozenset(model["data_classes_allowed"])  # the cell's own
    # Jev: disabled without a config entry; with one it is still the stub (nothing is sent)
    off = commands.connector(dep, "jev")
    assert isinstance(off, JevJudge) and off.config is None
    entry = SimpleNamespace(
        enabled=True,
        endpoint="https://example.invalid",
        token_file=None,
        data_classes_allowed=("public",),
    )
    on = commands.connector(command_dep(w, jev=entry), "jev")
    assert on.config is not None and on.config.enabled is True
    hold("JUDGE_NOT_CONFIGURED", on.ask, STATE, [YES])
    fault("JUDGE_UNKNOWN", commands.connector, dep, "gpt-anything")
    hold("CELL_UNKNOWN", commands.connector, dep, "llm_cell.no-such-cell")


def test_the_label_command_stores_operator_labelled_items_as_a_label_set(
    deployment: Any, tmp_path: Path
) -> None:
    from amplai_foundry.runtime.meta_commands import judges as commands

    w = build_world(deployment, tmp_path)
    dep = command_dep(w)
    entries = [
        {
            "item_id": f"i{n}",
            "state": {"text": f"case {n} {'Y' if n % 2 == 0 else 'N'}", "data_class": "internal"},
            "question": {"question_id": "q", "text": "Is it ambiguous?"},
            "label": n % 2 == 0,
        }
        for n in range(6)
    ]
    path = tmp_path / "labels.json"
    path.write_text(json.dumps({"items": entries}))
    out = commands.label(dep, "yes_no", path)
    assert out["items"] == 6 and out["question_type"] == "yes_no"
    stored = dep.store.get(dep.scope, "judge-label-set", out["label_set_ref"])
    assert stored["items"][0]["source"] == "operator"
    assert stored["items"][0]["labelled_by"]["kind"] == "human"  # the operator identity
    assert stored["items"][0]["question"]["type"] == "yes_no"
    state = json.loads(dep.artifacts.read(dep.scope, stored["items"][0]["state_ref"]))
    assert state == {"text": "case 0 Y", "data_class": "internal"}
    assert "case 0" not in json.dumps(stored)  # the state text lives in its artifact only
    assert commands.label_set_ref(dep, out["label_set_ref"]["id"]) == out["label_set_ref"]
    revision = out["label_set_ref"]["id"] + "@" + str(out["label_set_ref"]["revision"])
    assert commands.label_set_ref(dep, revision) == out["label_set_ref"]
    fault("NOT_FOUND", commands.label_set_ref, dep, "labels-yes_no-nothing")
    # a file that is not {"items": [...]} or an item without a state is refused
    path.write_text(json.dumps({"items": []}))
    fault("JUDGE_LABELS", commands.label, dep, "yes_no", path)
    path.write_text(json.dumps([entries[0]]))
    fault("JUDGE_LABELS", commands.label, dep, "yes_no", path)
    path.write_text(json.dumps({"items": [{"item_id": "x", "label": True}]}))
    fault("JUDGE_LABELS", commands.label, dep, "yes_no", path)


def test_the_list_command_shows_judges_label_sets_and_qualifications(
    deployment: Any, tmp_path: Path
) -> None:
    from amplai_foundry.runtime.meta_commands import judges as commands

    w = build_world(deployment, tmp_path)
    dep = command_dep(w)
    empty = commands.listing(dep)
    assert empty["judges"] == ["llm_cell." + CELL, "jev"] and empty["jev_enabled"] is False
    assert empty["label_sets"] == [] and empty["qualifications"] == []
    e = Env(w.rig.d)
    judge = Scripted(decide=exact)
    labels = label_set(e, items(40))
    qualify(e, judge, labels)
    shown = commands.listing(dep)
    assert [(q["judge_id"], q["question_type"], q["status"]) for q in shown["qualifications"]] == [
        ("scripted.a", "yes_no", "pass"),
    ]
    assert shown["qualifications"][0]["accuracy"] == 1.0 and shown["qualifications"][0]["ece_10"]
    assert [(s["question_type"], s["items"]) for s in shown["label_sets"]] == [("yes_no", 40)]
