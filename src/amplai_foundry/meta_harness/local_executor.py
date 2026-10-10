"""Offline experiment executor on the local product (Work 030 S4, D-089; Work 033 S8).

One trial is one real goal: a corpus task becomes a contract, the goal runs on a pinned
composition (an experiment arm) from the corpus's base commit through the ExecutionLoop with
publication off, and its result is graded from what the goal left: the task's hidden tests applied
to a scratch copy of the verified change, or the planner's questions for an ambiguity task. The
receipt binds exactly what ``EvaluationService`` re-checks (composition, task, repeat, mode,
success, safety, unknown effects, usage), and its first artifact is admitted with verifier trust.

A trial that could not be run to an answer (a driver fault, a held goal) is ``success=None``: the
analysis treats it as unknown, never as a pass or a failure of the candidate.

Work 033 S8 (interfaces.md §8.3, §8.4, IC-03, IC-18):

- corpus v2 tasks (``CorpusV2``) beside the Work 030 corpus; a task's app, base commit, grading and
  environment come from the task and its base.
- planner mode: ``real`` (the arm cell's own planner, ``product._planner``) for
  ``planner_questions`` tasks and for arms whose L1 decider or ``interpretation`` differs from v1,
  else ``fixed`` (``TrialPlanner``, the task's fixed contract).
- every trial goal carries a ``TrialContext``: its nodes claim ``sandbox:<app>:trial:<goal_id>``
  (IC-03), so trials of one app run concurrently; the trace flag and environment id ride along.
  That claim rests on trial goals never publishing, so no trial goal outlives its call: one the
  loop leaves open (a held goal keeps its approval for a requeue) is stopped by the executor.
- ``safety_failures`` and ``unknown_effects`` are counted from the goal's runs
  (``counters_source: run_records_v1``), never constants.
- ``self.trials`` is guarded by a lock; the executor is called from parallel trial threads (§8.4).

Operator decision (C), 2026-10-08: an answer-lookup attempt (a search outside the workspace,
reading git history beyond the base commit, a web search tool call) that a driver recorded for one
of the goal's runs (``agent_drivers/answer_lookup.py``; the worker copies it into the execution
head, ``worker.driver_lookups``) fails the trial: ``success`` False whatever the goal reached, and
the receipt's ``answer_lookup`` lists the evidence (run, dispatch, kind, rule, command text). It is
not a safety failure (it stops nothing); ``trial_metrics`` counts it as a hack-guard signal. The
goal's read-only turns count the same way: the planner turn (``planner_usage``) and each auxiliary
turn (``aux_usage`` entry ``usage``) keep their findings under ``answer_lookup``
(``runtime/execution/readonly_turn.py`` ``LOOKUP_KEY``); their receipt entries carry ``turn``
(``planner`` or ``aux:<role>``) instead of a run id.

Work 033 S12 (IC-17, provisional): the operator may be the nightly service identity
``amplai-meta-nightly`` (``meta_local.nightly_actor``); ``LocalExecutionService.approve`` accepts
it for trial goals only.

Work 033 S7b (IC-12, interfaces.md §10.5 step 6, §2.10): a task whose ``environment`` is a task
environment (a Terminal-Bench 2.0 task) runs on the arm composition's environment sibling
(``LocalExecutionService.env_sibling``: same manifest, the task image's environment, driver and
model records) and verifies with that environment's verifier profile, so the run's, the driver's
and the verifier's environment are one. A task environment the app has not installed, or one
without a qualified composition of the arm's cell (the missing pair), holds
ENVIRONMENT_UNQUALIFIED before any goal is submitted. With ``environment_digests`` (the stage
plan's pins) the executor first recomputes the task environment's digest with the evaluation
service's probe (``task_environment_digest``); on a mismatch, or a task environment the plan did
not pin, it runs nothing and returns ``success`` None with receipt ``environment_drift`` true, so
the task counts as missing. The receipt's ``environment_binding`` names the task environment and
the manifest digest (§2.3: the digest of the four carrier refs). ``tb2_tests`` grading needs the
TB2 test entry command, which is 확인 필요 (§14 Q7): a verified TB2 run is reported ``success``
None, never graded by a guessed command.
"""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ..evaluation.service import NOT_RUN_EVIDENCE, TrialObservation
from ..runtime.contracts.authority import Actor
from ..runtime.contracts.identity import canonical, digest, digest_bytes, new_id
from ..runtime.contracts.semantics import resolve_ref
from ..runtime.errors import Hold, RuntimeFault
from ..runtime.execution import policies, releases
from ..runtime.execution.loop import ExecutionLoop
from ..runtime.execution.meta_local import NIGHTLY_EXCLUDED, NIGHTLY_ID
from ..runtime.execution.product import LocalExecutionService, TrialContext
from ..runtime.goals.service import GoalService
from . import corpus_v2, local_corpus, trial_metrics
from .corpus_v2 import CorpusV2, TaskV2
from .local_corpus import Corpus, CorpusTask

if TYPE_CHECKING:
    from ..runtime.execution.cells import Cell
    from .manifest import ManifestService

# Statuses that mean the goal ran to an answer that says the task was not solved.
NOT_SOLVED = frozenset({"failed", "timed_out"})
# A trial goal never outlives its call (IC-03, ``_close``): plan statuses the loop leaves an ended
# goal in, plan statuses that wait for the operator (``loop.cancel`` ends them), and the runtime
# goal states of an ended goal (``service.end_goal``).
PLAN_ENDED = frozenset({"verified", "published", "failed", "cancelled", "timed_out"})
PLAN_WAITING = frozenset({"awaiting_approval", "needs_answers", "replan_failed"})
GOAL_ENDED = frozenset({"verified", "failed", "cancelled"})
BEHAVIOUR_VERIFIER = "unit"
COUNTERS_SOURCE = "run_records_v1"  # §2.10: safety/unknown counted from the goal's runs (§8.3)
LOOKUP_MAX = 50  # answer-lookup evidence entries kept in one receipt (decision (C))
# M6 (Work 033 S9): escalated revisions one trial follows; cascade.max_escalations is 1 (§5.2),
# and LocalExecutionService.escalate holds ESCALATION_LIMIT past the cascade's own limit
MAX_ESCALATIONS = 1
# TrialContext values when a call cannot be tied to one dispatching trial record (a direct call,
# or two in-flight trials of one case, repeat and composition), or its case has no split yet.
UNBOUND_ARM = "unbound"
UNASSIGNED_SPLIT = "unassigned"
DEVELOPMENT = "development"
UNKNOWN_DOMAIN = "unknown"  # IC-23: a case that names no domain (a Work 030 task names none)
UNKNOWN_SHA = "unknown"  # §8.5: a key built without the harness sha is never reused
SHA1 = re.compile(r"[0-9a-f]{40}")
CARRIER_FIELDS = (
    "prompt_bundle_ref",
    "context_policy_ref",
    "budget_policy_ref",
    "router_policy_ref",
)

_HARNESS_SHA: str | None = None
_HARNESS_LOCK = threading.Lock()


def harness_sha() -> str:
    """``git rev-parse HEAD`` of the checkout this code runs from, read once per process (§8.5);
    ``unknown`` when it cannot be read."""
    global _HARNESS_SHA
    with _HARNESS_LOCK:
        if _HARNESS_SHA is None:
            sha = UNKNOWN_SHA
            try:
                run = subprocess.run(
                    ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
                    capture_output=True, text=True, timeout=10, check=False,
                )  # fmt: skip
                if run.returncode == 0 and SHA1.fullmatch(run.stdout.strip()):
                    sha = run.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
            _HARNESS_SHA = sha
        return _HARNESS_SHA


def base_scope(tree: Path) -> list[str]:
    """``in_scope`` of a corpus base: its top-level directories as ``<name>/``, sorted, without
    hidden entries and ``__pycache__`` (the demo base gives ``['demo_app/', 'tests/']``)."""
    if not tree.is_dir():
        return []
    return sorted(
        p.name + "/"
        for p in tree.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name != "__pycache__"
    )


class TrialPlanner:
    """The deterministic stand-in for the model planner: a task's fixed contract.

    ``in_scope`` is derived per base from the base tree's top-level directories (clarification
    after W2 part 1); the Work 030 demo base keeps ``['demo_app/', 'tests/']``.
    """

    def __init__(
        self, corpus: Corpus | CorpusV2, behaviour_verifier: str = BEHAVIOUR_VERIFIER
    ) -> None:
        tasks: list[CorpusTask | TaskV2] = [*corpus.tasks]
        self.by_text = {task.contract_text(): task for task in tasks}
        self.behaviour_verifier = behaviour_verifier
        if isinstance(corpus, CorpusV2):
            scopes = {
                base_id: base_scope(corpus.root / base["dir"])
                for base_id, base in corpus.bases.items()
                if isinstance(base.get("dir"), str)
            }
            self.in_scope = {
                task.contract_text(): list(scopes.get(task.base_id, [])) for task in corpus.tasks
            }
        else:
            scope = base_scope(corpus.base)
            self.in_scope = {task.contract_text(): list(scope) for task in corpus.tasks}

    def bound_verifier(self, verifiers: Iterable[str]) -> str | None:
        """The app verifier a corpus task's behaviour acceptance names (operator decision
        2026-10-09): the configured ``behaviour_verifier`` when the app has it; else the app's
        only verifier; None (TRIAL_VERIFIER) when the app has none, or several and none is the
        configured one."""
        ids = list(verifiers)
        if self.behaviour_verifier in ids:
            return self.behaviour_verifier
        return ids[0] if len(ids) == 1 else None

    def draft(
        self, goal: str, app: str, verifiers: dict[str, str], workspace: Path, *, mode: str = "work"
    ) -> dict[str, Any]:
        task = self.by_text.get(goal)
        if task is None:
            raise Hold("TRIAL_TASK", "The goal text is not a corpus task contract")
        behaviour = self.bound_verifier(verifiers)
        if behaviour is None:
            raise Hold(
                "TRIAL_VERIFIER", "The app has no behaviour verifier for corpus tasks",
                details={"configured": self.behaviour_verifier, "verifiers": sorted(verifiers)},
            )  # fmt: skip
        acceptance = [{"statement": s, "verifier": behaviour} for s in task.acceptance]
        acceptance += [
            {"statement": description, "verifier": verifier}
            for verifier, description in verifiers.items()
            if verifier != behaviour
        ]
        draft = {
            "summary": task.task_id,
            "objective": task.objective,
            "in_scope": list(self.in_scope.get(goal, [])),
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
    return [_legacy_payload(corpus, task) for task in corpus.tasks]


def _legacy_payload(corpus: Corpus, task: CorpusTask) -> dict[str, Any]:
    return {
        "case_id": task.task_id,
        "difficulty": task.difficulty,
        "corpus_id": corpus.corpus_id,
        "base_commit": corpus.base_commit,
        "contract_text": task.contract_text(),
        "hidden_digest": digest({k: v.hex() for k, v in sorted(task.hidden.items())}),
    }


@dataclass(frozen=True)
class _Spec:
    """What one case is, whichever corpus it comes from."""

    task: CorpusTask | TaskV2
    app_id: str
    base_commit: str
    grading: str
    environment_id: str
    split: str | None
    corpus_version: str | None
    payload: dict[str, Any]

    @property
    def expected(self) -> str | None:
        """``ask`` / ``proceed`` of an ambiguity task (§10.2), else None."""
        ambiguity = getattr(self.task, "ambiguity", None)
        return ambiguity.get("expected") if isinstance(ambiguity, dict) else None


class LocalTrialExecutor:
    def __init__(
        self,
        service: LocalExecutionService,
        loop: ExecutionLoop,
        goals: GoalService,
        operator: Actor,
        corpus: CorpusV2 | Corpus,
        *,
        busy: Callable[[], bool] | None = None,
        behaviour_verifier: str = BEHAVIOUR_VERIFIER,
        manifests: ManifestService | None = None,
        traces: Any | None = None,  # TraceService (S13, meta_harness/traces.py)
        cells: dict[str, Cell] | None = None,
        environment_digests: dict[str, str] | None = None,
        environment_probe: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        """``busy`` (optional) tells whether a non-trial goal is running; a trial waits for it
        (TRIAL_BUSY). ``traces`` turns trace capture on for trial goals (S13);
        ``environment_digests`` are the stage plan's task-environment pins (S7b; None: no drift
        check, as for calibration; a dict, possibly empty, means the pins are in force and a
        task environment it does not name is drifted); ``environment_probe`` is the evaluation
        service's probe (``EvaluationService.environment_probe``; None digests the record
        itself, as that service does)."""
        if loop.publisher is not None:
            raise Hold("TRIAL_PUBLISH", "A trial loop must not publish")
        # IC-17 (S12, provisional): the nightly service identity runs trials too; its goal
        # approvals are refused by ``approve`` for anything but a trial goal
        nightly = operator.kind == "service" and operator.subject_id == NIGHTLY_ID
        if operator.kind != "human" and not nightly:
            raise Hold("TRIAL_OPERATOR", "Trials run under the operator's approved experiment")
        if nightly and operator.permissions & NIGHTLY_EXCLUDED:
            raise Hold("TRIAL_OPERATOR", "Not the nightly identity: it holds excluded permissions")
        self.service, self.loop, self.goals = service, loop, goals
        self.operator, self.corpus, self.busy = operator, corpus, busy
        self.manifests, self.traces = manifests, traces
        self.cells = dict(cells or {})
        self.environment_digests: dict[str, str] | None = (
            dict(environment_digests) if environment_digests is not None else None
        )
        self.environment_probe = environment_probe
        self.planner = TrialPlanner(corpus, behaviour_verifier)
        self.store, self.scope = service.store, service.scope
        self.artifacts = service.workspaces.artifacts
        self.harness_sha = harness_sha()  # read at boot (§8.5)
        self.trials: list[dict[str, Any]] = []
        self._lock = threading.Lock()  # self.trials and self._bound (trials run in threads)
        self._bound: set[str] = set()  # dispatching trial ids a running call is tied to

    # -- one trial -----------------------------------------------------------------------------
    def __call__(
        self, composition_ref: dict[str, Any], case: dict[str, Any], repeat: int, mode: str
    ) -> TrialObservation:
        """One trial. An exception it raises carries the store evidence of what the trial
        started (``evaluation.service.NOT_RUN_EVIDENCE``, operator decision 2026-10-09)."""
        submitted: dict[str, str] = {}
        checks_before = getattr(self.service, "base_check_starts", None)
        try:
            return self._trial(composition_ref, case, repeat, mode, submitted)
        except Exception as exc:
            self._attach_evidence(exc, submitted, checks_before)
            raise

    def _attach_evidence(
        self, exc: Exception, submitted: dict[str, str], checks_before: object
    ) -> None:
        """Set ``NOT_RUN_EVIDENCE`` on ``exc``: the goal this call submitted (``goal_id``, None:
        none), its ``planner_mode`` (None: no goal), ``base_checks`` (base-check suite runs the
        service started during the call, ``ProductService.base_check_starts``; a concurrent
        trial's count too, which only makes the services more conservative), the runs whose
        record names the goal as root goal (``runtime/execution/service.py`` claim) and their
        worker dispatches. Evidence that cannot be read is not attached (the services then keep
        the trial an unknown effect)."""
        try:
            checks_now = getattr(self.service, "base_check_starts", None)
            if type(checks_before) is not int or type(checks_now) is not int:
                return
            store, scope = self.store, self.scope
            goal_id = submitted.get("goal_id")
            runs = [] if goal_id is None else trial_metrics.goal_runs(store, scope, goal_id)
            dispatches = trial_metrics.run_dispatches(store, scope, runs)
            setattr(
                exc, NOT_RUN_EVIDENCE,
                {"goal_id": goal_id, "planner_mode": submitted.get("planner_mode"),
                 "base_checks": checks_now - checks_before, "runs": runs,
                 "dispatches": dispatches},
            )  # fmt: skip
        except Exception:  # evidence is optional; the original error propagates
            return

    def _trial(
        self,
        composition_ref: dict[str, Any],
        case: dict[str, Any],
        repeat: int,
        mode: str,
        submitted: dict[str, str],
    ) -> TrialObservation:
        if self.busy is not None and self.busy():
            raise Hold("TRIAL_BUSY", "Another goal is running; a trial waits for it")
        spec = self._spec(case)
        task_env = spec.environment_id != releases.APP_ENVIRONMENT
        if spec.grading == "tb2_tests" and not task_env:
            raise Hold(
                "TRIAL_ENVIRONMENT", "A tb2_tests task runs in its task environment (§10.5)",
                details={"environment_id": spec.environment_id, "grading": spec.grading},
            )  # fmt: skip
        if spec.app_id not in self.service.apps:
            raise Hold("TARGET_UNKNOWN", "Not an installed app", details=[spec.app_id])
        cell_id = trial_metrics.cell_of(self.service, spec.app_id, composition_ref)
        if cell_id is None:
            raise Hold(
                "COMPOSITION_PIN",
                "A pinned composition must be an installed one or its class-A candidate",
            )
        executed, binding = composition_ref, None
        if task_env:
            # IC-12 (S7b): the task environment's sibling, checked before any goal exists
            installed = self.service.apps[spec.app_id]
            self.service.environment_ref(installed, spec.environment_id)  # Hold: not installed
            binding = {
                "environment_id": spec.environment_id,
                "manifest_digest": manifest_digest(
                    self.store.get(self.scope, "harness-composition", composition_ref)
                ),
            }
            if self._drifted(installed, spec.environment_id):
                return self._drift(composition_ref, case, repeat, mode, spec, cell_id, binding)
            executed = self.service.env_sibling(installed, composition_ref, spec.environment_id)
        # operator decision 2026-10-08: the driver must declare how its web tools are off
        # before any goal exists (the loop checks again before any claim, for every revision)
        self._require_offline(executed)
        subject, arm, bound = self._bind(composition_ref, case, repeat)
        try:
            context = TrialContext(
                subject=subject,
                arm=arm,
                cell_id=cell_id,
                split=spec.split or UNASSIGNED_SPLIT,
                # §9.1, D-100: only development trials capture (the proposer's split, §9.4)
                capture_trace=self.traces is not None and spec.split == DEVELOPMENT,
                planner_mode=self._planner_mode(spec, composition_ref),
                environment_id=spec.environment_id,
                domain=self._domain(spec),
            )
            record = self._run_goal(executed, spec, context, submitted)
        finally:
            if bound is not None:
                with self._lock:
                    self._bound.discard(bound)
        return self._observe(
            composition_ref, case, repeat, mode, spec, context, record,
            executed=executed, binding=binding,
        )  # fmt: skip

    def _require_offline(self, composition_ref: dict[str, Any]) -> None:
        """Hold DRIVER_WEB_UNDECLARED when the composition's registered driver port does not
        declare how its web tools are turned off (``agent_drivers/offline.py``). A driver with no
        registered port is left to the dispatch (DRIVER_NOT_INSTALLED), as before."""
        from ..agent_drivers import offline

        composition = self.store.get(self.scope, "harness-composition", composition_ref)
        registry = getattr(self.loop.coordinator, "registry", None)
        installed = getattr(registry, "installed", None)
        ref = composition.get("driver_profile_ref")
        port = installed(self.scope, ref) if callable(installed) and ref else None
        if port is not None:
            offline.require_port(port)

    # -- IC-12 task environments: drift (S7b, §10.5 step 6) ------------------------------------
    def task_environment_digest(self, installed: Any, environment_id: str) -> str:
        """The digest the stage plan pins for a task environment (§2.9 ``environment_digests``):
        ``digest(probe(record))`` of its environment record, the rule
        ``EvaluationService.freeze`` applies to the experiment's ``environment_ref``."""
        return task_environment_digest(
            self.store, self.scope, self.service.environment_ref(installed, environment_id),
            self.environment_probe,
        )  # fmt: skip

    def _drifted(self, installed: Any, environment_id: str) -> bool:
        """With stage pins: the task environment is not pinned, or its digest differs now."""
        if self.environment_digests is None:
            return False
        pinned = self.environment_digests.get(environment_id)
        return pinned is None or pinned != self.task_environment_digest(installed, environment_id)

    def _drift(
        self,
        composition_ref: dict[str, Any],
        case: dict[str, Any],
        repeat: int,
        mode: str,
        spec: _Spec,
        cell_id: str,
        binding: dict[str, Any],
    ) -> TrialObservation:
        """Nothing runs: ``success`` None, receipt ``environment_drift`` true, no usage (§10.5)."""
        composition = self.store.get(self.scope, "harness-composition", composition_ref)
        snapshot, effort = self._snapshot(composition)
        strategy = trial_metrics.strategy_of(self.service, composition_ref) or ""
        usage: dict[str, Any] = {
            "input_tokens": 0, "output_tokens": 0, "cost_microunits": 0,
            "usage_status": "measured",
        }  # fmt: skip
        task = spec.task
        receipt = {
            "task_id": task.task_id,
            "repeat": repeat,
            "composition_ref": composition_ref,
            "mode": mode,
            "success": None,
            "safety_failures": 0,
            "unknown_effects": 0,
            **usage,
            "scope": self.scope.wire(),
            "goal_id": None,
            "goal_status": None,
            "goal_reason": "environment_drift",
            "corpus_id": spec.payload.get("corpus_id", self.corpus.corpus_id),
            "base_commit": spec.base_commit,
            "executed_composition_ref": composition_ref,  # nothing ran in a sibling
            "escalation_chain": [],
            "environment_binding": binding,
            "environment_drift": True,
            "cell_id": cell_id,
            "strategy": strategy,
            "grading": spec.grading,
            "planner": {"mode": "fixed", "questions": 0, "usage": None},
            "counters_source": COUNTERS_SOURCE,
            "decisions": [],
            "trace_ref": None,
            "cache_key": self._cache_key(
                composition, {**snapshot, "reasoning_profile": effort}, spec, case, strategy,
                repeat,
            ),
            "corpus_version": spec.corpus_version,
            "split": spec.split,
            "harness_sha": self.harness_sha,
            "model_snapshot": snapshot,
        }  # fmt: skip
        proof: dict[str, Any] = {
            "task_id": task.task_id,
            "goal_id": None,
            "attempts": [],
            "environment_drift": {
                "environment_id": binding["environment_id"],
                "pinned": (self.environment_digests or {}).get(binding["environment_id"]),
            },
        }
        receipt_ref = self.artifacts.admit(
            self.scope, canonical(receipt), "application/json", trust="verifier"
        )
        proof_ref = self.artifacts.admit(
            self.scope, canonical(proof), "application/json", trust="verifier"
        )
        with self._lock:
            self.trials.append({"task_id": task.task_id, "repeat": repeat, **receipt})
        return TrialObservation(
            None, (receipt_ref, proof_ref), cost_microunits=0, input_tokens=0, output_tokens=0,
            usage_status="measured",
        )  # fmt: skip

    def _observe(
        self,
        composition_ref: dict[str, Any],
        case: dict[str, Any],
        repeat: int,
        mode: str,
        spec: _Spec,
        context: TrialContext,
        record: dict[str, Any],
        *,
        executed: dict[str, Any] | None = None,
        binding: dict[str, Any] | None = None,
    ) -> TrialObservation:
        executed = executed or composition_ref  # IC-12: the environment sibling that ran
        status = record.get("status")
        if record["ran"] and status == "cancelled":
            raise Hold("TRIAL_CANCELLED", "The trial goal was cancelled")
        questions: list[str] = record["questions"]
        detail: dict[str, Any] = {}
        success: bool | None
        if not record["ran"]:
            if spec.grading == "planner_questions" and (spec.expected == "ask" or questions):
                # graded from the planner's questions alone (§8.3): nothing ran
                outcome = self._grade_questions(spec, questions)
                success, detail = outcome.success, {"detail": outcome.detail}
            else:
                success = None  # the planner asked back on a task graded by hidden tests
        elif status == "verified" and spec.grading == "tb2_tests":
            # §10.5: graded by the task tests in the task image, whose entry command is 확인 필요
            # (§14 Q7); not graded by a guessed command, so no answer about the candidate
            success = None
            detail = {"detail": "tb2_tests: not graded (TB2 test entry command, §14 Q7)"}
        elif status == "verified":
            outcome = self._judge(spec, record, questions)
            detail = {
                "hidden_passed": outcome.hidden_passed,
                "visible_passed": outcome.visible_passed,
                "detail": outcome.detail,
            }
            success = outcome.success
        elif status in NOT_SOLVED:
            success = False
        else:
            success = None  # held, replan_failed, ...: no answer about the candidate
        plan = self.service.plan_record(record["goal_id"])
        counters = self._counters(plan)
        lookups: list[dict[str, Any]] = counters["answer_lookup"]
        if lookups:
            # decision (C): an answer-lookup attempt fails the trial, whatever the goal reached
            success = False
            detail = {**detail, "detail": "answer lookup: " + lookups[0]["rule"]}
        real = context.planner_mode == "real"
        # M6 (Work 033 S9): an escalated trial spent the revisions on every cell it ran on
        ran = [
            *((plan.get("previous_attempts") or []) if plan.get("escalations") else []),
            *(plan.get("attempts") or []),
        ]
        # §8.3: runs + planner + the strategy's auxiliary read-only turns (every revision)
        usage = self._usage(ran, plan.get("planner_usage"), real=real, aux=plan.get("aux_usage"))
        # the snapshot and cache key of what ran: the arm composition, or its environment sibling
        # (the task image's driver and model records, IC-12)
        composition = self.store.get(self.scope, "harness-composition", executed)
        snapshot, effort = self._snapshot(composition)
        strategy = trial_metrics.strategy_of(self.service, composition_ref) or ""
        chain = self._escalation_chain(plan, executed)
        task = spec.task
        receipt = {
            "task_id": task.task_id,
            "repeat": repeat,
            "composition_ref": composition_ref,
            "mode": mode,
            "success": success,
            "safety_failures": counters["safety_failures"],
            "unknown_effects": counters["unknown_effects"],
            **usage,
            "scope": self.scope.wire(),
            "goal_id": record.get("goal_id"),
            "goal_status": status,
            "goal_reason": record.get("reason"),
            "corpus_id": spec.payload.get("corpus_id", self.corpus.corpus_id),
            "base_commit": spec.base_commit,
            **detail,
            # receipt v2 (interfaces.md §2.10), descriptive. After an escalation (M6) the graded
            # revision ran on the cell sibling of the arm composition; the receipt then names it
            # only together with the chain of every revision's cell and composition
            "executed_composition_ref": chain[-1]["composition_ref"] if chain else executed,
            "escalation_chain": chain,
            # IC-12 (S7b): the task environment and manifest it ran in; None in the app's own
            "environment_binding": binding,
            "environment_drift": False,
            "cell_id": context.cell_id,
            "strategy": strategy,
            "grading": spec.grading,
            "planner": {
                "mode": context.planner_mode,
                "questions": len(questions),
                "usage": plan.get("planner_usage") if real else None,
                # operator decision 2026-10-09: the verifier the fixed draft bound the task's
                # behaviour acceptance to (the configured one, or the app's only verifier)
                "behaviour_verifier": self._behaviour_verifier(spec) if not real else None,
            },
            "counters_source": COUNTERS_SOURCE,
            # decision (C): the answer-lookup evidence of the goal's runs (empty: none)
            "answer_lookup": lookups,
            # §6.8: the goal's decision records; §2.10: the graded run's harness trace
            "decisions": [dict(r) for r in plan.get("decisions") or [] if isinstance(r, dict)],
            "trace_ref": self._trace_ref(ran, context),
            "cache_key": self._cache_key(
                composition,
                {**snapshot, "reasoning_profile": effort},
                spec,
                case,
                strategy,
                repeat,
            ),
            "corpus_version": spec.corpus_version,
            "split": spec.split,
            "harness_sha": self.harness_sha,
            "model_snapshot": snapshot,
        }
        proof = {
            "task_id": task.task_id,
            "goal_id": record.get("goal_id"),
            "attempts": [
                {k: a.get(k) for k in ("run_id", "outcome", "reason", "seconds")} for a in ran
            ],
            "counters": counters["detail"],
        }
        receipt_ref = self.artifacts.admit(
            self.scope, canonical(receipt), "application/json", trust="verifier"
        )
        proof_ref = self.artifacts.admit(
            self.scope, canonical(proof), "application/json", trust="verifier"
        )
        with self._lock:
            self.trials.append({"task_id": task.task_id, "repeat": repeat, **receipt})
        return TrialObservation(
            success,
            (receipt_ref, proof_ref),
            safety_failures=counters["safety_failures"],
            unknown_effects=counters["unknown_effects"],
            cost_microunits=usage["cost_microunits"],
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            usage_status=usage["usage_status"],
            # decision (C): the evidence count the services bind to the receipt's list
            answer_lookup=len(lookups),
            # decision (B): the reported parts of an unknown usage, a lower bound of its charge
            known_tokens=usage.get("known_tokens"),
            known_cost_microunits=usage.get("known_cost_microunits"),
        )

    def _behaviour_verifier(self, spec: _Spec) -> str | None:
        """The verifier ``TrialPlanner.draft`` binds behaviour acceptance to for this case's app
        (the app's verifiers now; ``_run_goal`` planned against the same installed app)."""
        installed = self.service.apps.get(spec.app_id)
        if installed is None:
            return None
        return self.planner.bound_verifier(v.id for v in installed.config.verifiers)

    def _spec(self, case: dict[str, Any]) -> _Spec:
        """What the case is, from the corpus loaded now, checked against the frozen case.

        A frozen case (``EvaluationService`` and ``CalibrationService`` pass the eval-corpus case)
        carries ``artifact_ref``, the admitted canonical payload of its task at freeze time
        (``corpus_v2.case_payload``, ``corpus_cases``). The trial is built from the loaded task of
        the case id, so a task edited, removed or re-versioned after the freeze would run under
        the frozen case id: Hold CORPUS_CHANGED (as ``LocalMetaOps.check_corpus``) unless the
        loaded task's payload digest equals the frozen one. A case without ``artifact_ref`` (a
        direct call) has no frozen payload to compare with."""
        corpus = self.corpus
        frozen = "artifact_ref" in case
        try:
            if isinstance(corpus, CorpusV2):
                task_v2 = corpus.task(case["case_id"])
                # a task of another set is frozen in that set's corpus (``corpus_v2.for_set``: the
                # regression set is amplai-regression-v1, §10.6), so its payload names that corpus;
                # pilot 2026-10-11: computed from the whole corpus, every regression trial was
                # held CORPUS_CHANGED
                source = corpus if task_v2.set == "main" else corpus_v2.for_set(corpus, task_v2.set)
                payload = corpus_v2.case_payload(source, task_v2)
            else:
                task = corpus.task(case["case_id"])
                payload = _legacy_payload(corpus, task)
        except local_corpus.CorpusError as exc:
            if not frozen:
                raise
            # the frozen task is gone, or its base commit is unreadable now
            raise Hold(
                "CORPUS_CHANGED",
                "The task loaded now differs from the frozen corpus case",
                details={"case_id": case.get("case_id"), "corpus_error": exc.code},
            ) from exc
        if frozen:
            self._check_frozen(case, payload)
        if isinstance(corpus, CorpusV2):
            base = corpus.bases.get(task_v2.base_id) or {}
            app_id = base.get("app_id")
            if not isinstance(app_id, str) or not app_id:
                raise Hold("TRIAL_TASK", "The task's base names no app", details=task_v2.base_id)
            return _Spec(
                task_v2, app_id, payload["base_commit"], task_v2.grading,
                task_v2.environment_id, case.get("split") or task_v2.split, corpus.version,
                payload,
            )  # fmt: skip
        return _Spec(
            task, corpus.app_id, corpus.base_commit, "pytest_hidden", "app", case.get("split"),
            None, payload,
        )  # fmt: skip

    @staticmethod
    def _check_frozen(case: dict[str, Any], payload: dict[str, Any]) -> None:
        """Hold CORPUS_CHANGED unless ``digest_bytes(canonical(payload))`` (the digest the
        artifact store gave the frozen payload, ``cas.admit``) equals ``case.artifact_ref``."""
        ref = case.get("artifact_ref")
        frozen = ref.get("digest") if isinstance(ref, dict) else None
        loaded = digest_bytes(canonical(payload))
        if not isinstance(frozen, str) or frozen != loaded:
            raise Hold(
                "CORPUS_CHANGED",
                "The task loaded now differs from the frozen corpus case; freeze a new corpus",
                details={"case_id": case.get("case_id"), "frozen_digest": frozen,
                         "loaded_digest": loaded},
            )  # fmt: skip

    @staticmethod
    def _domain(spec: _Spec) -> str:
        """IC-23: the case's domain for the trial's L1/L2 decisions: a corpus v2 task's
        ``domain`` (``TaskV2.domain``, checked against ``corpus_v2.DOMAINS`` by the loader); a
        Work 030 task (``CorpusTask``: id, difficulty, objective, acceptance, hidden, reference)
        names none, so ``"unknown"``."""
        value = getattr(spec.task, "domain", None)
        return value if isinstance(value, str) and value else UNKNOWN_DOMAIN

    def _planner_mode(
        self, spec: _Spec, composition_ref: dict[str, Any]
    ) -> Literal["fixed", "real"]:
        """``real`` for a planner-questions task and for an arm whose L1 decider or
        ``interpretation`` differs from v1 (§8.3); ``fixed`` otherwise."""
        if spec.grading == "planner_questions":
            return "real"
        try:
            composition = self.store.get(self.scope, "harness-composition", composition_ref)
            router = policies.router_policy(
                self.store, self.scope, composition["router_policy_ref"]
            )
        except (Hold, RuntimeFault, KeyError):
            return "fixed"  # a router the loop cannot read is held by the loop before any claim
        if router.interpretation != policies.V1["interpretation"] or router.deciders.get("L1"):
            return "real"
        return "fixed"

    def _bind(
        self, composition_ref: dict[str, Any], case: dict[str, Any], repeat: int
    ) -> tuple[dict[str, str], str, str | None]:
        """The dispatching trial record this call serves (``subject``, ``arm``, its id).

        The executor's signature carries no trial id (§3.10, unchanged): ``EvaluationService``
        and ``CalibrationService`` write a ``dispatching`` trial head (task, repeat, arm or cell,
        owner epoch) before they call the executor, so the call is tied to the one such head of
        this case and repeat whose composition is ``composition_ref``. None or several: unbound.
        """
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT kind,id,data FROM heads WHERE tenant=? AND project=? "
                "AND kind IN ('eval-trial','calibration-trial') AND state='dispatching' "
                "ORDER BY rowid",
                self.scope.keys(),
            ).fetchall()
        found: list[tuple[str, dict[str, str], str]] = []
        for row in rows:
            data = json.loads(row["data"])
            if (
                data.get("task_id") != case.get("case_id")
                or data.get("repeat") != repeat
                or data.get("owner_epoch") != self.store.epoch
            ):
                continue
            try:
                if row["kind"] == "eval-trial":
                    plan = self.store.get(self.scope, "eval-experiment", data["experiment_ref"])
                    if self._arm_ref(plan, data.get("arm")) != composition_ref:
                        continue
                    subject = {"experiment_id": plan["experiment_id"], "trial_id": row["id"]}
                    found.append((row["id"], subject, str(data["arm"])))
                else:
                    plan = self.store.get(
                        self.scope, "calibration-plan", data["calibration_plan_ref"]
                    )
                    cell_refs = (plan.get("composition_refs") or {}).get(data.get("cell_id"))
                    if composition_ref not in (cell_refs or []):
                        continue
                    subject = {
                        "calibration_plan_id": data["calibration_plan_ref"]["id"],
                        "trial_id": row["id"],
                    }
                    found.append((row["id"], subject, trial_metrics.CALIBRATION_ARM))
            except (Hold, RuntimeFault, KeyError, TypeError):
                continue
        with self._lock:
            free = [f for f in found if f[0] not in self._bound]
            if len(free) != 1:
                return {}, UNBOUND_ARM, None
            trial_id, subject, arm = free[0]
            self._bound.add(trial_id)
        return subject, arm, trial_id

    def _arm_ref(self, plan: dict[str, Any], arm: Any) -> dict[str, Any] | None:
        if arm in ("baseline", "candidate"):
            ref: dict[str, Any] = plan[arm + "_ref"]
            return ref
        if arm == "reference":
            _, analysis = resolve_ref(self.store, self.scope, plan["analysis_plan_ref"])
            reference = (analysis.get("policy") or {}).get("reference_arm") or {}
            found: dict[str, Any] | None = reference.get("composition_ref")
            return found
        return None

    def _run_goal(
        self,
        composition_ref: dict[str, Any],
        spec: _Spec,
        context: TrialContext,
        submitted: dict[str, str],
    ) -> dict[str, Any]:
        """Submit, plan (fixed or real planner), and unless the plan already decides the grade,
        approve and run the goal. A goal that does not run is closed (cancelled); a goal the loop
        leaves open, or an error leaves approved, is closed too (``_close``). An error while
        planning (a plan-time Hold such as TRIAL_VERIFIER) closes the goal as well. The submitted
        goal id and the planner mode go into ``submitted`` (the caller's evidence of an
        error)."""
        goal = self.goals.submit(
            self.service.actors.service,
            text=spec.task.contract_text(),
            target_hints=[spec.app_id],
            key="trial-" + new_id("key"),
        )
        goal_id: str = goal["goal_id"]
        submitted.update(goal_id=goal_id, planner_mode=context.planner_mode)
        try:
            planned = self.service.plan(
                goal_id,
                composition=composition_ref,
                planner=self.planner if context.planner_mode == "fixed" else None,
                revision=spec.base_commit,
                trial=context,
            )
            questions = [str(q) for q in (planned.get("draft") or {}).get("questions") or []]
            # asked back (needs_answers), or an "ask" task graded by its questions: nothing runs
            at_plan = planned.get("status") != "awaiting_approval" or (
                spec.grading == "planner_questions" and spec.expected == "ask"
            )
            if at_plan:
                self.loop.cancel(self.operator, goal_id)
                return {**planned, "goal_id": goal_id, "ran": False, "questions": questions}
            self.service.approve(self.operator, goal_id)
            record: dict[str, Any] = self.loop.run_goal(goal_id)
            # M6 (IC-05, Work 033 S9): a cascade that failed its attempts on its cell compiled
            # the next revision on the next cell; the trial follows it under the experiment's
            # operator identity. ``escalate`` holds past the cascade's max_escalations (1).
            for _ in range(MAX_ESCALATIONS):
                if record.get("status") != "awaiting_approval" or not record.get("escalation"):
                    break
                self.service.approve(self.operator, goal_id)
                record = self.loop.run_goal(goal_id)
        except Exception as exc:
            # the error is the caller's answer; the goal must not stay approved behind it
            with contextlib.suppress(Exception):
                self._close(goal_id, f"trial error: {getattr(exc, 'code', type(exc).__name__)}")
            raise
        # graded from what the loop returned; the stored plan may then say it was closed
        self._close(goal_id, str(record.get("reason") or "trial goal closed"))
        return {**record, "goal_id": goal_id, "ran": True, "questions": questions}

    def _close(self, goal_id: str, reason: str) -> None:
        """Leave no trial goal that a loop could run again (IC-03).

        A trial goal claims ``sandbox:<app>:trial:<goal_id>`` on the premise that it never
        publishes, and its result is this call's observation. The loop keeps the approval of a
        goal whose claim failed before any attempt (``held``) so that ``reconcile`` requeues it,
        and a replanned goal waits for a new approval; a later loop with a publisher would then
        run such a goal outside the experiment. So a goal left ``held`` with a valid approval or
        an open runtime goal, or left ``approved``/``running`` by an error, is stopped ``held``
        (approval revoked, runtime goal ended, the loop's reason and attempts kept); a waiting
        goal is cancelled. A goal the loop already ended is left as it is. A goal whose planning
        failed before any plan record was saved is still ``draft``; its runtime goal is ended
        cancelled.
        """
        try:
            plan = self.service.plan_record(goal_id)
        except RuntimeFault:
            # planning stopped before it saved a plan record: no approval, no claim, and the
            # runtime goal is still ``draft``; it ends cancelled so no draft trial goal remains
            self.service.runtime.end_goal(
                self.service.actors.service, goal_id, outcome="cancelled", reason=reason[:600]
            )
            return
        status = plan.get("status")
        if status in PLAN_ENDED:
            return
        if status in PLAN_WAITING:
            self.loop.cancel(self.operator, goal_id)
            return
        if status == "held":
            goal_state = self.store.head(self.scope, "goal", goal_id)["state"]
            if goal_state in GOAL_ENDED and not self.loop._approval_valid(plan):
                return  # the loop stopped it: revoked and ended
            reason = str(plan.get("reason") or reason)
        self.loop._stop_goal(goal_id, "held", reason[:600], plan.get("attempts") or [])

    # -- grading ---------------------------------------------------------------------------------
    @staticmethod
    def _grade_questions(spec: _Spec, questions: list[str]) -> local_corpus.Outcome:
        """``planner_questions`` graded before anything ran: expected ``ask`` (whole-word terms,
        ``corpus_v2.grade``) or expected ``proceed`` with questions asked (a failure)."""
        assert isinstance(spec.task, TaskV2)
        # both branches return before any test runs, so no workspace is read
        return corpus_v2.grade(spec.task, Path("/nonexistent"), planner_questions=questions)

    def _judge(
        self, spec: _Spec, record: dict[str, Any], questions: list[str]
    ) -> local_corpus.Outcome:
        """The task's grader on a scratch copy of the verified change."""
        attempts = record.get("attempts") or []
        change = next((a["change"] for a in reversed(attempts) if a.get("change")), None)
        if change is None:
            return local_corpus.Outcome(False, False, "no change recorded")
        raw = self.artifacts.read(self.scope, change)
        path, _patch = self.service.workspaces.materialize_change(
            self.scope, new_id("trial-ws"), raw
        )
        try:
            task = spec.task
            if isinstance(task, TaskV2):
                # operator decision 2026-10-09 (clean-room grading): the copy's tests and test
                # configuration are the base's, so no trial change can alter what grades it
                workspaces = self.service.workspaces
                reverted = trial_metrics.cleanroom_paths(workspaces.change_paths(path))
                workspaces.restore_base(path, reverted)
                asked = questions if task.grading == "planner_questions" else None
                outcome = corpus_v2.grade(task, path, planner_questions=asked)
                if not reverted:
                    return outcome
                note = "clean-room reverted: " + ", ".join(reverted[:20])
                return replace(outcome, detail=f"{note}; {outcome.detail}")
            return local_corpus.judge(task, path)
        finally:
            self.service.workspaces.discard(path)

    # -- counters from the run records (§8.3, IC-18) ---------------------------------------------
    def _counters(self, plan: dict[str, Any]) -> dict[str, Any]:
        """``unknown_effects``: effect heads of the goal's runs still dispatched/unknown plus worker
        executions held without a confirmed process stop. ``safety_failures``: attempts whose
        patch edits or deletes an existing test file, touches a protected path or adds or edits a
        test-run file (``trial_metrics.grading_paths``, decision 2026-10-09), plus runs whose
        artifact admission was refused with SECRET_DETECTED. ``answer_lookup`` (decision (C)):
        the answer-lookup evidence the worker kept on the runs' execution heads, each entry with
        its run id, then that of the goal's read-only turns (``_turn_lookups``), at most
        ``LOOKUP_MAX`` in all."""
        store, scope = self.store, self.scope
        run_ids = trial_metrics.goal_run_ids(store, scope, plan)
        executions = trial_metrics.run_executions(store, scope, run_ids)
        effects = trial_metrics.open_effects(store, scope, run_ids)
        unstopped = [
            e["id"]
            for e in executions
            if e["state"] == "held" and e["data"].get("process_stopped") is False
        ]
        attempts = [*(plan.get("previous_attempts") or []), *(plan.get("attempts") or [])]
        edits = []
        for attempt in attempts:
            if not attempt.get("change"):
                continue
            value = json.loads(self.artifacts.read(scope, attempt["change"]))
            stats = trial_metrics.diff_stats(self.artifacts.read(scope, value["patch"]))
            protected = trial_metrics.protected_paths(stats["paths"])
            grading = trial_metrics.grading_paths(stats["paths"])
            if stats["tests_changed"] or protected or grading:
                edits.append(
                    {"run_id": attempt.get("run_id"), "tests_changed": stats["tests_changed"],
                     "protected": protected, "grading_files": grading}
                )  # fmt: skip
        secret = sorted(
            {str(e["data"].get("run_id")) for e in executions
             if e["data"].get("hold_code") == trial_metrics.SECRET_CODE}
            | {str(a.get("run_id")) for a in attempts
               if a.get("reason") == trial_metrics.SECRET_CODE}
        )  # fmt: skip
        lookups = [
            {"run_id": e["data"].get("run_id"), **entry}
            for e in executions
            for entry in e["data"].get("answer_lookup") or []
            if isinstance(entry, dict)
        ]
        lookups = [*lookups, *self._turn_lookups(plan)][:LOOKUP_MAX]
        return {
            "safety_failures": len(edits) + len(secret),
            "unknown_effects": len(effects) + len(unstopped),
            "answer_lookup": lookups,
            "detail": {
                "runs": len(run_ids),
                "open_effects": effects,
                "unstopped_dispatches": unstopped,
                "test_or_protected_edits": edits,
                "secret_refused_runs": secret,
                "answer_lookup": len(lookups),
            },
        }

    @staticmethod
    def _turn_lookups(plan: dict[str, Any]) -> list[dict[str, Any]]:
        """Decision (C): the answer-lookup findings of the goal's read-only turns, kept in their
        usage under ``answer_lookup`` (``readonly_turn.LOOKUP_KEY``): the planner turn's
        (``planner_usage``, the last revision's, as ``product.py`` stores it), then each
        auxiliary turn's (``aux_usage``), each entry naming its ``turn``."""
        found: list[dict[str, Any]] = []
        planner = plan.get("planner_usage")
        if isinstance(planner, dict):
            found += [
                {"turn": "planner", **entry}
                for entry in planner.get("answer_lookup") or []
                if isinstance(entry, dict)
            ]
        for aux in plan.get("aux_usage") or []:
            usage = aux.get("usage") if isinstance(aux, dict) else None
            if not isinstance(usage, dict):
                continue
            found += [
                {"turn": f"aux:{aux.get('role')}", "node_id": aux.get("node_id"), **entry}
                for entry in usage.get("answer_lookup") or []
                if isinstance(entry, dict)
            ]
        return found

    def _usage(
        self,
        attempts: list[dict[str, Any]],
        planner: dict[str, Any] | None,
        *,
        real: bool,
        aux: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Tokens and cost summed over the goal's attempts, in real-planner mode the planner's
        turn, and the strategy's auxiliary read-only turns (``aux_usage``, Work 033 S9); unknown
        unless every part says (§8.3: runs + planner + auxiliary read-only turns). An unknown
        usage also carries ``known_tokens`` / ``known_cost_microunits``: the counts the other
        parts did report (decision (B); the services charge at least that)."""
        parts: list[dict[str, Any]] = []
        for attempt in attempts:
            try:
                run = self.store.head(self.scope, "run", attempt["run_id"])
                parts.append(run["data"]["record"].get("usage") or {})
            except Exception:
                parts.append({})
        if real:
            # the planner turn's own provider-reported counts (readonly_turn.py); no cost
            parts.append({"status": "measured", **(planner or {})} if planner else {})
        for entry in aux or []:
            # strategy_runner.AuxLedger entries (§5.3): ``tokens`` None is a turn that may have
            # spent tokens without reporting them (failed, abandoned), so the usage is unknown;
            # ``usage`` None with ``tokens`` 0 is a turn that never started and spent nothing
            if entry.get("tokens") is None:
                parts.append({})
            elif isinstance(entry.get("usage"), dict):
                # the read-only turn's provider-reported counts (readonly_turn.py); no cost
                parts.append({
                    "status": "measured",
                    "input_tokens": entry["usage"].get("input_tokens"),
                    "output_tokens": entry["usage"].get("output_tokens"),
                })  # fmt: skip
        totals = {"input_tokens": 0, "output_tokens": 0, "cost_microunits": 0}
        statuses: set[str] = set()
        known = bool(parts)
        # decision (B): what the parts did report, kept when another part is unknown so the
        # charge of the unknown usage is never below it (a real overrun stays an overrun)
        reported = {"tokens": 0, "cost": 0}
        for usage in parts:
            statuses.add(str(usage.get("status")))
            for key in ("input_tokens", "output_tokens"):
                if type(usage.get(key)) is int:
                    reported["tokens"] += usage[key]
            if type(usage.get("cost_microunits")) is int:
                reported["cost"] += usage["cost_microunits"]
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
                "known_tokens": reported["tokens"],
                "known_cost_microunits": reported["cost"],
            }
        status = "measured" if statuses == {"measured"} else "estimated"
        return {
            **totals,
            "cost_microunits": None if totals["cost_microunits"] < 0 else totals["cost_microunits"],
            "usage_status": status,
        }

    # -- receipt v2 facts (§2.10, §8.5) ------------------------------------------------------------
    def _trace_ref(self, ran: list[dict[str, Any]], context: TrialContext) -> dict[str, Any] | None:
        """The receipt's ``trace_ref``: the ``harness-trace`` admitted for the trial's last run
        (the graded attempt), or None (no run, no capture, or a ``trace-drop``). A planner-turn
        trace (``<goal_id>.planner``) is linked by its record's ``goal_id``, not here."""
        if self.traces is None or not context.capture_trace:
            return None
        run_ids = [a.get("run_id") for a in ran if isinstance(a.get("run_id"), str)]
        if not run_ids:
            return None
        try:
            found: dict[str, Any] | None = self.traces.of_run(run_ids[-1])
        except (Hold, RuntimeFault):
            return None
        return found

    def _escalation_chain(
        self, plan: dict[str, Any], composition_ref: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Every revision of an escalated trial goal (M6, IC-05), first to last: its contract
        revision, cell and composition; empty without an escalation.

        The first revision ran on the arm composition (the trial pins it); the last on the
        plan record's composition (``escalate``: the cell sibling on ``to_cell``). A revision
        between them would have no stored composition, but a cascade allows exactly one
        escalation (``policies``: ``max_escalations`` 1, ``MAX_ESCALATIONS``), so there is none;
        such a revision would be recorded with ``composition_ref`` null, never guessed."""
        escalations = [e for e in plan.get("escalations") or [] if isinstance(e, dict)]
        if not escalations:
            return []
        contracts = [*(e.get("previous_contract_ref") for e in escalations), plan["contract_ref"]]
        cells = [escalations[0].get("from_cell"), *(e.get("to_cell") for e in escalations)]
        last = len(contracts) - 1
        chain = []
        for index, (contract_ref, cell) in enumerate(zip(contracts, cells, strict=True)):
            revision = None
            if isinstance(contract_ref, dict):
                contract = self.store.get(self.scope, "goal-contract", contract_ref)
                revision = contract.get("revision")
            if index == 0:
                ref: dict[str, Any] | None = composition_ref
            elif index == last:
                ref = (plan.get("composition") or {}).get("ref")
            else:
                ref = None
            chain.append(
                {"revision": revision, "contract_ref": contract_ref, "cell_id": cell,
                 "composition_ref": ref}
            )  # fmt: skip
        return chain

    def _snapshot(self, composition: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        """The receipt's ``model_snapshot`` (§2.10) and the model profile's reasoning profile."""
        model = self.store.get(self.scope, "model-profile", composition["model_profile_ref"])
        driver = self.store.get(
            self.scope, "driver-capabilities", composition["driver_profile_ref"]
        )
        _, environment = resolve_ref(self.store, self.scope, composition["sandbox_profile_ref"])
        snapshot = {
            "provider_model_id": model.get("provider_model_id"),
            "driver_version": driver.get("driver_version"),
            "image": environment.get("image"),
        }
        return snapshot, model.get("reasoning_profile")

    def _cache_key(
        self,
        composition: dict[str, Any],
        snapshot: dict[str, Any],
        spec: _Spec,
        case: dict[str, Any],
        strategy: str,
        repeat: int,
    ) -> str:
        """§8.5: manifest, cell, task artifact, corpus version, harness sha, strategy, repeat."""
        artifact = (case.get("artifact_ref") or {}).get("digest") or digest(spec.payload)
        return digest(
            {
                "manifest_digest": digest({k: composition.get(k) for k in CARRIER_FIELDS}),
                "cell": {
                    k: snapshot.get(k)
                    for k in ("provider_model_id", "reasoning_profile", "driver_version", "image")
                },
                "task_artifact_digest": artifact,
                "corpus_version": spec.corpus_version,
                "harness_sha": self.harness_sha,
                "strategy": strategy,
                "repeat_slot": repeat,
            }
        )


def manifest_digest(composition: dict[str, Any]) -> str:
    """§2.3: the digest of the four carrier refs (``harness-manifest.manifest_digest``)."""
    return digest({k: composition.get(k) for k in CARRIER_FIELDS})


def task_environment_digest(
    store: Any,
    scope: Any,
    environment_ref: dict[str, Any],
    probe: Callable[[dict[str, Any]], dict[str, Any]] | None,
) -> str:
    """``digest(probe(env))`` of an environment record (``digest(env)`` without a probe): the
    rule ``EvaluationService`` applies at freeze and before each dispatch
    (``evaluation/service.py:492-493,605-608``), applied to a task environment (§10.5)."""
    _kind, environment = resolve_ref(store, scope, environment_ref)
    return digest(probe(environment) if probe is not None else environment)


def summary(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A compact table of what a set of trials did (for a run log; no hidden test content)."""
    keys = ("task_id", "repeat", "success", "goal_status", "goal_reason")
    return [{k: t.get(k) for k in keys} for t in trials]
