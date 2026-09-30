"""Work 030 S4 (D-089) — the offline experiment executor on the local product.

A corpus task becomes a real goal on a pinned composition and a pinned base commit, runs through
the ExecutionLoop with publication off, and is judged by hidden tests applied to a scratch copy of
the verified change. Stand-ins as in the rc06 rig (a scripted agent); the real trials are recorded
under specs/030-meta-harness-live/runs/.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.evaluation.receipts import read_receipt
from amplai_foundry.evaluation.service import EvaluationService
from amplai_foundry.meta_harness import local_corpus
from amplai_foundry.meta_harness.local_executor import LocalTrialExecutor, corpus_cases
from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.execution import prompts
from amplai_foundry.runtime.execution.loop import ExecutionLoop
from rc06_rig import git, rig_with_codex

VERIFIER = "check"  # the rig app's behaviour verifier (value() must return 2)

PASS_TEST = "from app import value\n\n\ndef test_two() -> None:\n    assert value() == 2\n"
DOC_TEST = (
    "from app import value\n\n\ndef test_two_with_a_doc() -> None:\n"
    '    assert value() == 2\n    assert value.__doc__ == "two"\n'
)
REFERENCE = "def value():\n    return 2\n"
DOC_REFERENCE = 'def value():\n    "two"\n    return 2\n'


def make_corpus(root: Path, base_commit: str) -> local_corpus.Corpus:
    (root / "base").mkdir(parents=True)
    (root / "base" / "app.py").write_text("def value():\n    return 1\n")
    tasks = {
        "t-pass": (PASS_TEST, REFERENCE),
        "t-doc": (DOC_TEST, DOC_REFERENCE),
    }
    for task_id, (hidden, reference) in tasks.items():
        folder = root / "tasks" / task_id
        (folder / "hidden").mkdir(parents=True)
        (folder / "reference").mkdir(parents=True)
        (folder / "hidden" / "test_t.py").write_text(hidden)
        (folder / "reference" / "app.py").write_text(reference)
        (folder / "task.json").write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "difficulty": "small",
                    "objective": "Make value() in app.py return 2.",
                    "acceptance": [f"value() returns 2 ({task_id})"],
                }
            )
        )
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "corpus_id": "mini",
                "app_id": "app",
                "base_commit": base_commit,
                "task_ids": list(tasks),
            }
        )
    )
    return local_corpus.load(root)


def executor_for(
    deployment: Any, tmp_path: Path, mode: str
) -> tuple[LocalTrialExecutor, Any, Any, local_corpus.Corpus]:
    rig, loop, container = rig_with_codex(deployment, tmp_path, mode)
    corpus = make_corpus(tmp_path / "corpus", git(rig.repo, "rev-parse", "HEAD").strip())
    trial_loop = ExecutionLoop(rig.service, loop.coordinator, publisher=None)
    executor = LocalTrialExecutor(
        rig.service, trial_loop, rig.d.goals, rig.operator, corpus, behaviour_verifier=VERIFIER
    )
    return executor, rig, container, corpus


def baseline(rig: Any) -> dict[str, Any]:
    ref: dict[str, Any] = rig.service.apps["app"].compositions["codex-cli"]
    return ref


def candidate(rig: Any, suffix: str = "cand") -> dict[str, Any]:
    d, service = rig.d, rig.service
    base = d.store.get(d.scope, "harness-composition", baseline(rig))
    bundle = service._put(
        prompts.KIND,
        f"implementer-{suffix}",
        prompts.bundle(f"implementer-{suffix}", ["Implement it in {app_id}."], "test"),
    )
    value = {
        **base,
        "composition_id": base["composition_id"] + "__" + suffix,
        "prompt_bundle_ref": bundle,
    }
    ref: dict[str, Any] = service._put("harness-composition", value["composition_id"], value)
    return ref


def case(task_id: str) -> dict[str, Any]:
    return {"case_id": task_id}


def bound(rig: Any, obs: Any, arm: dict[str, Any], task_id: str, repeat: int) -> None:
    """What EvaluationService re-checks before it accepts a trial."""
    service = EvaluationService(
        rig.d.store,
        rig.d.contracts,
        rig.d.artifacts,
        approval_check=lambda *a: {},
        executor_id="local-offline-executor",
    )
    service._validate_observation(rig.d.scope, obs, arm, task_id, repeat, "sandbox_rerun")


def test_the_corpus_is_fair_before_any_trial(deployment: Any, tmp_path: Path) -> None:
    _executor, _rig, _container, corpus = executor_for(deployment, tmp_path, "right")
    results = local_corpus.validate(corpus, tmp_path / "scratch")
    assert all(row["fair"] for row in results.values()), results


def test_a_solved_task_is_a_success_with_a_bound_receipt(deployment: Any, tmp_path: Path) -> None:
    executor, rig, container, _corpus = executor_for(deployment, tmp_path, "right")
    arm = baseline(rig)
    obs = executor(arm, case("t-pass"), 0, "sandbox_rerun")
    assert obs.success is True
    bound(rig, obs, arm, "t-pass", 0)
    receipt = read_receipt(rig.d.artifacts.read(rig.d.scope, obs.artifact_refs[0], trusted=True))
    assert receipt["hidden_passed"] is True and receipt["goal_status"] == "verified"
    assert receipt["base_commit"] == executor.corpus.base_commit
    # the hidden tests never reached the agent's prompt or the record
    assert "test_two" not in json.dumps(receipt)
    assert all("test_two" not in prompt for prompt in container.prompts)


def test_the_hidden_tests_are_the_judge_not_only_the_app_verifier(
    deployment: Any, tmp_path: Path
) -> None:
    executor, rig, _container, _corpus = executor_for(deployment, tmp_path, "right")
    obs = executor(baseline(rig), case("t-doc"), 0, "sandbox_rerun")
    # the app's own verifier passes (value() == 2) but the hidden test wants a docstring
    assert obs.success is False
    receipt = read_receipt(rig.d.artifacts.read(rig.d.scope, obs.artifact_refs[0], trusted=True))
    assert receipt["goal_status"] == "verified"
    assert receipt["hidden_passed"] is False and receipt["visible_passed"] is True


def test_a_goal_that_never_passes_is_a_failure_not_an_unknown(
    deployment: Any, tmp_path: Path
) -> None:
    executor, rig, _container, _corpus = executor_for(deployment, tmp_path, "always-wrong")
    obs = executor(baseline(rig), case("t-pass"), 0, "sandbox_rerun")
    assert obs.success is False
    receipt = read_receipt(rig.d.artifacts.read(rig.d.scope, obs.artifact_refs[0], trusted=True))
    assert receipt["goal_status"] == "failed" and "hidden_passed" not in receipt


def test_a_driver_fault_is_unknown_not_a_verdict_on_the_candidate(
    deployment: Any, tmp_path: Path
) -> None:
    executor, rig, _container, _corpus = executor_for(deployment, tmp_path, "crash")
    arm = baseline(rig)
    obs = executor(arm, case("t-pass"), 0, "sandbox_rerun")
    assert obs.success is None
    bound(rig, obs, arm, "t-pass", 0)
    assert obs.usage_status == "unknown" and obs.input_tokens is None


def test_usage_is_summed_over_the_goal_and_bound(deployment: Any, tmp_path: Path) -> None:
    executor, rig, _container, _corpus = executor_for(deployment, tmp_path, "right")
    obs = executor(baseline(rig), case("t-pass"), 0, "sandbox_rerun")
    assert (obs.input_tokens, obs.output_tokens) == (10, 5)
    assert obs.usage_status in {"measured", "estimated"}


def test_an_arm_pins_its_composition_and_a_stranger_is_refused(
    deployment: Any, tmp_path: Path
) -> None:
    executor, rig, container, _corpus = executor_for(deployment, tmp_path, "right")
    cand = candidate(rig)
    obs = executor(cand, case("t-pass"), 0, "sandbox_rerun")
    goal = executor.trials[-1]["goal_id"]
    planned = rig.service.plan_record(goal)["composition"]
    assert planned["pinned"] is True and planned["ref"] == cand
    assert obs.success is True
    # the same goal text on the baseline pins the baseline
    executor(baseline(rig), case("t-pass"), 1, "sandbox_rerun")
    other = rig.service.plan_record(executor.trials[-1]["goal_id"])["composition"]
    assert other["ref"] == baseline(rig)
    # a ref that is neither an installed composition nor its class-A candidate is refused
    with pytest.raises(Hold) as refused:
        executor(rig.service.apps["app"].router_ref, case("t-pass"), 2, "sandbox_rerun")
    assert refused.value.code == "COMPOSITION_PIN"
    assert len(container.prompts) == 2  # the refused trial never started a run


def test_a_trial_loop_cannot_publish_and_needs_a_human_operator(
    deployment: Any, tmp_path: Path
) -> None:
    executor, rig, _container, corpus = executor_for(deployment, tmp_path, "right")
    publishing = ExecutionLoop(rig.service, executor.loop.coordinator, publisher=lambda g: {})
    with pytest.raises(Hold) as published:
        LocalTrialExecutor(rig.service, publishing, rig.d.goals, rig.operator, corpus)
    assert published.value.code == "TRIAL_PUBLISH"
    service_actor = rig.actors.service
    with pytest.raises(Hold) as human:
        LocalTrialExecutor(rig.service, executor.loop, rig.d.goals, service_actor, corpus)
    assert human.value.code == "TRIAL_OPERATOR"


def test_a_busy_product_makes_the_trial_wait(deployment: Any, tmp_path: Path) -> None:
    executor, rig, container, _corpus = executor_for(deployment, tmp_path, "right")
    executor.busy = lambda: True
    with pytest.raises(Hold) as busy:
        executor(baseline(rig), case("t-pass"), 0, "sandbox_rerun")
    assert busy.value.code == "TRIAL_BUSY" and container.prompts == []


def test_frozen_case_payloads_carry_the_contract_and_a_digest_never_the_tests(
    deployment: Any, tmp_path: Path
) -> None:
    _executor, _rig, _container, corpus = executor_for(deployment, tmp_path, "right")
    cases = corpus_cases(corpus)
    assert [c["case_id"] for c in cases] == ["t-pass", "t-doc"]
    text = json.dumps(cases)
    assert "test_two" not in text and "value.__doc__" not in text
    assert all(c["hidden_digest"].startswith("sha256:") for c in cases)
    assert all(c["base_commit"] == corpus.base_commit for c in cases)
