"""Offline experiment executor on the local product (Work 030 S4, D-089).

One trial is one real goal: a corpus task becomes a fixed contract (no model planner), the goal
runs on a pinned composition (an experiment arm) from the corpus's base commit through the
ExecutionLoop with publication off, and its result is judged by the corpus's hidden tests applied
to a scratch copy of the verified change. The receipt binds exactly what
``EvaluationService`` re-checks (composition, task, repeat, mode, success, usage), and its first
artifact is admitted with verifier trust.

A trial that could not be run to an answer (a driver fault, a held goal) is ``success=None``: the
analysis treats it as unknown, never as a pass or a failure of the candidate.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..evaluation.service import TrialObservation
from ..runtime.contracts.authority import Actor
from ..runtime.contracts.identity import canonical, new_id
from ..runtime.errors import Hold
from ..runtime.execution.loop import ExecutionLoop
from ..runtime.execution.product import LocalExecutionService
from ..runtime.goals.service import GoalService
from . import local_corpus
from .local_corpus import Corpus, CorpusTask

# Statuses that mean the goal ran to an answer that says the task was not solved.
NOT_SOLVED = frozenset({"failed", "timed_out"})
BEHAVIOUR_VERIFIER = "unit"


class TrialPlanner:
    """The deterministic stand-in for the model planner: a task's fixed contract."""

    def __init__(self, corpus: Corpus, behaviour_verifier: str = BEHAVIOUR_VERIFIER) -> None:
        self.by_text = {task.contract_text(): task for task in corpus.tasks}
        self.behaviour_verifier = behaviour_verifier

    def draft(
        self, goal: str, app: str, verifiers: dict[str, str], workspace: Path, *, mode: str = "work"
    ) -> dict[str, Any]:
        task = self.by_text.get(goal)
        if task is None:
            raise Hold("TRIAL_TASK", "The goal text is not a corpus task contract")
        if self.behaviour_verifier not in verifiers:
            raise Hold("TRIAL_VERIFIER", "The app has no behaviour verifier for corpus tasks")
        acceptance = [
            {"statement": s, "verifier": self.behaviour_verifier} for s in task.acceptance
        ]
        acceptance += [
            {"statement": description, "verifier": verifier}
            for verifier, description in verifiers.items()
            if verifier != self.behaviour_verifier
        ]
        draft = {
            "summary": task.task_id,
            "objective": task.objective,
            "in_scope": ["demo_app/", "tests/"],
            "non_goals": ["no change outside the stated behaviour"],
            "constraints": ["keep the existing public behaviour and tests passing"],
            "acceptance": acceptance,
            "risk": "low",
            "task_class": "logic_change",
            "assumptions": [],
            "questions": [],
        }
        return {"draft": draft, "usage": None}

    def draft_multi(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise Hold("TRIAL_ONE_APP", "A corpus trial targets exactly one app")


def corpus_cases(corpus: Corpus) -> list[dict[str, Any]]:
    """The frozen-corpus case payloads (contract text and the digest of the hidden tests, never
    the hidden tests themselves), in corpus order."""
    from ..runtime.contracts.identity import digest

    return [
        {
            "case_id": task.task_id,
            "difficulty": task.difficulty,
            "corpus_id": corpus.corpus_id,
            "base_commit": corpus.base_commit,
            "contract_text": task.contract_text(),
            "hidden_digest": digest({k: v.hex() for k, v in sorted(task.hidden.items())}),
        }
        for task in corpus.tasks
    ]


class LocalTrialExecutor:
    def __init__(
        self,
        service: LocalExecutionService,
        loop: ExecutionLoop,
        goals: GoalService,
        operator: Actor,
        corpus: Corpus,
        *,
        busy: Callable[[], bool] | None = None,
        behaviour_verifier: str = BEHAVIOUR_VERIFIER,
    ) -> None:
        if loop.publisher is not None:
            raise Hold("TRIAL_PUBLISH", "A trial loop must not publish")
        if operator.kind != "human":
            raise Hold("TRIAL_OPERATOR", "Trials run under the operator's approved experiment")
        self.service, self.loop, self.goals = service, loop, goals
        self.operator, self.corpus, self.busy = operator, corpus, busy
        self.planner = TrialPlanner(corpus, behaviour_verifier)
        self.store, self.scope = service.store, service.scope
        self.artifacts = service.workspaces.artifacts
        self.trials: list[dict[str, Any]] = []

    # -- one trial -----------------------------------------------------------------------------
    def __call__(
        self, composition_ref: dict[str, Any], case: dict[str, Any], repeat: int, mode: str
    ) -> TrialObservation:
        if self.busy is not None and self.busy():
            raise Hold("TRIAL_BUSY", "Another goal is running; a trial waits for it")
        task = self.corpus.task(case["case_id"])
        record = self._run_goal(composition_ref, task)
        status = record.get("status")
        if status == "cancelled":
            raise Hold("TRIAL_CANCELLED", "The trial goal was cancelled")
        outcome, detail = None, {}
        if status == "verified":
            outcome = self._judge(task, record)
            detail = {
                "hidden_passed": outcome.hidden_passed,
                "visible_passed": outcome.visible_passed,
                "detail": outcome.detail,
            }
            success: bool | None = outcome.success
        elif status in NOT_SOLVED:
            success = False
        else:
            success = None  # held, replan_failed, ...: no answer about the candidate
        usage = self._usage(record.get("attempts") or [])
        receipt = {
            "task_id": task.task_id,
            "repeat": repeat,
            "composition_ref": composition_ref,
            "mode": mode,
            "success": success,
            "safety_failures": 0,
            "unknown_effects": 0,
            **usage,
            "scope": self.scope.wire(),
            "goal_id": record.get("goal_id"),
            "goal_status": status,
            "goal_reason": record.get("reason"),
            "corpus_id": self.corpus.corpus_id,
            "base_commit": self.corpus.base_commit,
            **detail,
        }
        proof = {
            "task_id": task.task_id,
            "goal_id": record.get("goal_id"),
            "attempts": [
                {k: a.get(k) for k in ("run_id", "outcome", "reason", "seconds")}
                for a in record.get("attempts") or []
            ],
        }
        receipt_ref = self.artifacts.admit(
            self.scope, canonical(receipt), "application/json", trust="verifier"
        )
        proof_ref = self.artifacts.admit(
            self.scope, canonical(proof), "application/json", trust="verifier"
        )
        self.trials.append({"task_id": task.task_id, "repeat": repeat, **receipt})
        return TrialObservation(
            success,
            (receipt_ref, proof_ref),
            safety_failures=0,
            unknown_effects=0,
            cost_microunits=usage["cost_microunits"],
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            usage_status=usage["usage_status"],
        )

    def _run_goal(self, composition_ref: dict[str, Any], task: CorpusTask) -> dict[str, Any]:
        app = self.corpus.app_id
        submitted = self.goals.submit(
            self.service.actors.service,
            text=task.contract_text(),
            target_hints=[app],
            key="trial-" + new_id("key"),
        )
        goal_id: str = submitted["goal_id"]
        self.service.plan(
            goal_id,
            composition=composition_ref,
            planner=self.planner,
            revision=self.corpus.base_commit,
        )
        self.service.approve(self.operator, goal_id)
        record: dict[str, Any] = self.loop.run_goal(goal_id)
        return {**record, "goal_id": goal_id}

    # -- judging -------------------------------------------------------------------------------
    def _judge(self, task: CorpusTask, record: dict[str, Any]) -> local_corpus.Outcome:
        """The hidden tests on a scratch copy of the verified change."""
        attempts = record.get("attempts") or []
        change = next((a["change"] for a in reversed(attempts) if a.get("change")), None)
        if change is None:
            return local_corpus.Outcome(False, False, "no change recorded")
        raw = self.artifacts.read(self.scope, change)
        path, _patch = self.service.workspaces.materialize_change(
            self.scope, new_id("trial-ws"), raw
        )
        try:
            return local_corpus.judge(task, path)
        finally:
            self.service.workspaces.discard(path)

    def _usage(self, attempts: list[dict[str, Any]]) -> dict[str, Any]:
        """Tokens and cost summed over the goal's attempts; unknown unless every attempt says."""
        totals = {"input_tokens": 0, "output_tokens": 0, "cost_microunits": 0}
        statuses: set[str] = set()
        known = bool(attempts)
        for attempt in attempts:
            try:
                run = self.store.head(self.scope, "run", attempt["run_id"])
                usage = run["data"]["record"].get("usage") or {}
            except Exception:
                usage = {}
            statuses.add(str(usage.get("status")))
            counted = (
                usage.get("input_tokens") is not None and usage.get("output_tokens") is not None
            )
            if not counted:
                known = False
                continue
            totals["input_tokens"] += usage["input_tokens"]
            totals["output_tokens"] += usage["output_tokens"]
            if usage.get("cost_microunits") is None:
                totals["cost_microunits"] = -1
            elif totals["cost_microunits"] >= 0:
                totals["cost_microunits"] += usage["cost_microunits"]
        if not known:
            return {
                "input_tokens": None,
                "output_tokens": None,
                "cost_microunits": None,
                "usage_status": "unknown",
            }
        status = "measured" if statuses == {"measured"} else "estimated"
        return {
            **totals,
            "cost_microunits": None if totals["cost_microunits"] < 0 else totals["cost_microunits"],
            "usage_status": status,
        }


def summary(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A compact table of what a set of trials did (for a run log; no hidden test content)."""
    keys = ("task_id", "repeat", "success", "goal_status", "goal_reason")
    return [{k: t.get(k) for k in keys} for t in trials]
