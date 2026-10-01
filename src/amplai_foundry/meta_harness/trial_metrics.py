"""Metrics of experiment trials beyond the success rate (D-094). Descriptive only.

Everything here is read back from what a trial already stored: its receipt, its goal's attempts
and change, each run's usage and usage detail. Nothing re-runs an agent, so the same metrics apply
to trials recorded before this module existed (those only lack the cache breakdown).

Per trial:
- ``verified_hidden_fail``: the goal passed its own verifiers but the hidden tests failed (a change
  fitted to what the agent could check; the gap SWE-bench-style hidden tests exist to catch)
- ``tests_added`` / ``tests_changed``: new test files, and existing test files edited or deleted
  (only the second is a warning sign)
- diff size: files, lines added and removed
- tokens, seconds and the API-equivalent cost (``evaluation.pricing``)

Per arm: success count, the two counts above, and the tokens, seconds and cost per solved task.
None of these decide a verdict; the pre-registered analysis does (D-093).

Work 033 S8 (interfaces.md §2.10, ``plan.md`` §8.1, §9.10): ``trial`` adds the ``trial-metrics``
fields (source, split, stage, cell, strategy, token classes, verification time, what the strategy
did, hack-guard signals); ``record`` stores them as a ``trial-metrics`` record; ``summarize`` adds a
per-strategy breakdown per arm; ``guards`` compares the arms' guard signals at screening. The run
helpers below are also what the trial executor counts safety failures and unknown effects from
(§8.3, IC-18).

Work 033 S9: what the strategy did (escalations, reviewer rounds, fix requests, best-of-n first
pass, sub-agents, integration conflicts, re-verifications, executor turns) comes from the plan
record's ``strategy_metrics`` (``StrategyRunner.metrics``), counted over every revision of the goal,
so an escalated trial counts the attempts, runs and cells of both revisions. ``turns`` is the
number of executor turns AMPLAI dispatched (first turns and follow-ups); provider-internal turns
are not recorded (``provider_turns`` null until §14 Q14).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import PurePosixPath
from statistics import mean, median
from typing import Any

from jsonschema import Draft202012Validator

from ..evaluation import pricing
from ..runtime.contracts.identity import now
from ..runtime.contracts.semantics import resolve_ref
from ..runtime.errors import Hold, RuntimeFault
from ..runtime.execution.product import LocalExecutionService
from ..runtime.execution.worker import USAGE_DETAIL_KIND
from ..runtime.storage.store import Scope, Store

DIFF_FILE = re.compile(r"^diff --git a/(\S+) b/(\S+)$")

KIND = "trial-metrics"
SCHEMA = "amplai.trial-metrics.v1"
TRIAL_KINDS = ("eval-trial", "calibration-trial")
SPLITS = ("development", "validation", "holdout")
STAGES = ("screening", "focused", "ablation", "holdout")
PHASES = ("calibration", "stage", "drift", "screening_design", "search", "confirmation")
# A calibration trial has no experiment arm (§2.8); its rows carry this arm label.
CALIBRATION_ARM = "calibration"
# The token classes of a run's usage detail (agent_drivers/protocol.py USAGE_DETAIL_FIELDS):
# cached input is Codex `cached_input_tokens` / Claude `cache_read_input_tokens`; reasoning is
# reported by Codex only (`reasoning_output_tokens`), so it is null for a Claude run.
CACHED_FIELD = {"codex": "cached_input_tokens", "claude": "cache_read_input_tokens"}
REASONING_FIELD = {"codex": "reasoning_output_tokens"}
# Unknown-effect states of an effect head (runtime/execution/service.py:976-985).
OPEN_EFFECT_STATES = ("dispatched", "unknown")
SECRET_CODE = "SECRET_DETECTED"  # runtime/evidence/cas.py:82-87
# What a trial-metrics row takes from the strategy's own record (``StrategyRunner.metrics``, the
# plan record's ``strategy_metrics``, Work 033 S9), counted over every revision of the goal.
STRATEGY_FIELDS = (
    "turns", "escalations", "reviewer_rounds", "fix_requests", "best_of_n_first_pass",
    "sub_agents", "integration_conflicts", "re_verifications",
    # vote (§5.2 row 10, M4, S9b): candidates run, the turns beyond each attempt's own turn and
    # the selected candidate index; optional in the record (rows written before S9b lack them)
    "candidates", "candidate_turns", "vote_selected",
)  # fmt: skip
VOTE_FIELDS = ("candidates", "candidate_turns", "vote_selected")


def _vote_fields(strategy: dict[str, Any]) -> dict[str, Any]:
    """The vote fields of a trial row from the strategy's metrics: ``vote_selected`` there is
    one index per voting attempt (``StrategyRunner.metrics``); the row keeps the last one (the
    graded revision's attempt; vote has one attempt per revision, S9b) or null."""
    selected = [s for s in strategy.get("vote_selected") or [] if isinstance(s, int)]
    return {
        "candidates": int(strategy.get("candidates") or 0),
        "candidate_turns": int(strategy.get("candidate_turns") or 0),
        "vote_selected": selected[-1] if selected else None,
    }


def diff_stats(patch: bytes) -> dict[str, Any]:
    """Files and added/removed lines of a git patch (binary files count as a file, no lines).

    Test files are split by what happened to them: adding a new test is ordinary work, while
    editing or deleting a test that existed is the move a change fitted to its checks makes.
    """
    status: dict[str, str] = {}
    current = None
    added = removed = 0
    for line in patch.decode(errors="replace").splitlines():
        match = DIFF_FILE.match(line)
        if match:
            current = match.group(2)
            status.setdefault(current, "modified")
        elif current and line.startswith("new file mode"):
            status[current] = "added"
        elif current and line.startswith("deleted file mode"):
            status[current] = "deleted"
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    tests = {p: s for p, s in status.items() if "tests" in PurePosixPath(p).parts}
    return {
        "files": len(status),
        "paths": list(status),
        "lines_added": added,
        "lines_removed": removed,
        "tests_added": sorted(p for p, s in tests.items() if s == "added"),
        "tests_changed": sorted(p for p, s in tests.items() if s != "added"),
    }


def protected_paths(paths: Iterable[str]) -> list[str]:
    """The paths with a component in the protected set of ``runtime/execution/strategies.py``
    (``PROTECTED``, lines 16-28): a trial change there is a safety failure (§8.3)."""
    from ..runtime.execution.strategies import PROTECTED

    return sorted(p for p in paths if set(PurePosixPath(p).parts) & PROTECTED)


# -- what a goal ran ----------------------------------------------------------------------------
def goal_run_ids(store: Store, scope: Scope, plan: dict[str, Any]) -> list[str]:
    """Every run of a goal: the plan's attempts (this and earlier revisions) and every run the
    current graph's works were claimed for (a claimed run discarded before its turn included)."""
    ids: dict[str, None] = {}
    for attempt in [*(plan.get("previous_attempts") or []), *(plan.get("attempts") or [])]:
        if attempt.get("run_id"):
            ids[attempt["run_id"]] = None
    if plan.get("graph_ref"):
        graph = store.get(scope, "workgraph", plan["graph_ref"])
        for node in graph["nodes"]:
            try:
                work = store.head(scope, "work", node["work_id"])
            except RuntimeFault:
                continue
            for run_id in work["data"].get("run_ids") or []:
                ids[run_id] = None
    return list(ids)


def run_executions(store: Store, scope: Scope, run_ids: Iterable[str]) -> list[dict[str, Any]]:
    """The worker-execution heads of those runs (one per dispatch, ``worker.py``), with ``id``."""
    wanted = list(run_ids)
    if not wanted:
        return []
    marks = ",".join("?" for _ in wanted)
    with store._lock:
        rows = store.conn.execute(
            "SELECT dispatch_id FROM worker_dispatch WHERE tenant=? AND project=? "
            f"AND run_id IN ({marks}) ORDER BY rowid",
            (*scope.keys(), *wanted),
        ).fetchall()
    out = []
    for row in rows:
        try:
            head = store.head(scope, "worker-execution", row["dispatch_id"])
        except RuntimeFault:
            continue  # dispatched, never taken by a worker: nothing started
        out.append({"id": row["dispatch_id"], **head})
    return out


def open_effects(store: Store, scope: Scope, run_ids: Iterable[str]) -> list[str]:
    """Effect heads of those runs still ``dispatched`` or ``unknown`` (the query of
    ``runtime/execution/service.py:976-985``)."""
    wanted = set(run_ids)
    if not wanted:
        return []
    with store._lock:
        rows = store.conn.execute(
            "SELECT id,data FROM heads WHERE tenant=? AND project=? AND kind='effect' "
            "AND state IN ('dispatched','unknown') ORDER BY rowid",
            scope.keys(),
        ).fetchall()
    return [
        r["id"]
        for r in rows
        if (json.loads(r["data"]).get("request") or {}).get("run_id") in wanted
    ]


def strategy_of(service: LocalExecutionService, composition_ref: dict[str, Any]) -> str | None:
    """The execution strategy a composition declares first (``execution_strategy.enabled[0]``;
    v1: ``repair_loop``). A trial runs exactly that strategy or is held before any claim
    (``StrategyRunner.choose``, ``loop._policies``), so for a trial this is the strategy that ran
    or was refused."""
    from ..runtime.execution import policies

    try:
        composition = service.store.get(service.scope, "harness-composition", composition_ref)
        budget = policies.budget_policy(
            service.store, service.scope, composition, ceiling=service.budget.wire()
        )
    except (Hold, RuntimeFault):
        return None
    enabled = budget.execution_strategy.get("enabled") or []
    return str(enabled[0]) if enabled else None


def cell_of(
    service: LocalExecutionService, app_id: str, composition_ref: dict[str, Any]
) -> str | None:
    """The cell of an installed composition or of its class-A candidate (``pin_allowed``)."""
    from ..runtime.execution import releases

    installed = service.apps.get(app_id)
    if installed is None:
        return None
    return releases.pin_allowed(
        service.store, service.scope, installed.compositions, composition_ref
    )


def _utc(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


# -- the record ---------------------------------------------------------------------------------
_REF = {
    "type": "object",
    "required": ["id", "revision", "digest"],
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string"},
        "revision": {"type": "integer", "minimum": 1},
        "digest": {"type": "string"},
    },
}
_COUNT = {"type": "integer", "minimum": 0}
_OPT_COUNT = {"type": ["integer", "null"], "minimum": 0}
_OPT_REF = {"oneOf": [_REF, {"type": "null"}]}
RECORD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema", "scope", "trial_ref", "experiment_ref", "calibration_plan_ref", "source",
        "split", "stage", "task_id", "domain", "arm", "cell_id", "strategy", "success",
        "hidden_passed", "verified_hidden_fail", "tokens", "api_cost", "wall_seconds",
        "verification_seconds", "cells_used", "agent_calls", "turns", "attempts_used",
        "escalations", "reviewer_rounds", "fix_requests", "best_of_n_first_pass", "nodes",
        "sub_agents", "integration_conflicts", "re_verifications", "guards", "phase", "fidelity",
        "computed_at",
    ],
    "properties": {
        "schema": {"const": SCHEMA},
        "scope": {"type": "object"},
        "trial_ref": _REF,
        "experiment_ref": _OPT_REF,
        "calibration_plan_ref": _OPT_REF,
        "source": {"enum": ["experiment", "calibration"]},
        "split": {"enum": list(SPLITS)},
        "stage": {"enum": [*STAGES, None]},
        "task_id": {"type": "string", "minLength": 1},
        "domain": {"type": "string"},
        "arm": {"type": "string", "minLength": 1},
        "cell_id": {"type": "string"},
        "strategy": {"type": "string"},
        "success": {"type": ["boolean", "null"]},
        "hidden_passed": {"type": ["boolean", "null"]},
        "verified_hidden_fail": {"type": "boolean"},
        "tokens": {
            "type": "object",
            "additionalProperties": False,
            "required": ["input", "output", "cached_input", "reasoning"],
            "properties": {
                k: _OPT_COUNT for k in ("input", "output", "cached_input", "reasoning")
            },
        },
        "api_cost": {"type": "object", "required": ["status"]},
        "wall_seconds": {"type": "number", "minimum": 0},
        "verification_seconds": {"type": ["number", "null"], "minimum": 0},
        "cells_used": {"type": "array", "items": {"type": "string"}},
        "agent_calls": _COUNT,
        "turns": _OPT_COUNT,
        "attempts_used": _COUNT,
        "escalations": _COUNT,
        "reviewer_rounds": _COUNT,
        "fix_requests": _COUNT,
        "best_of_n_first_pass": _OPT_COUNT,
        "nodes": _COUNT,
        "sub_agents": _COUNT,
        "integration_conflicts": _COUNT,
        "re_verifications": _COUNT,
        # optional (§5.2 row 10): 0 / 0 / null for a strategy other than vote
        "candidates": _COUNT,
        "candidate_turns": _COUNT,
        "vote_selected": _OPT_COUNT,
        "guards": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "ask_back", "edit_files", "edit_lines", "broken_tool_calls", "test_file_edits",
                "verified_hidden_fail",
            ],
            "properties": {
                "ask_back": {"type": "boolean"},
                "edit_files": _COUNT,
                "edit_lines": _COUNT,
                "broken_tool_calls": _OPT_COUNT,
                "test_file_edits": _COUNT,
                "verified_hidden_fail": {"type": "boolean"},
            },
        },
        "phase": {"enum": list(PHASES)},
        "fidelity": {
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["rung", "tasks"],
                    "properties": {"rung": _COUNT, "tasks": _COUNT},
                },
                {"type": "null"},
            ]
        },
        "computed_at": {"type": "string", "minLength": 1},
    },
}  # fmt: skip


def validate_record(value: dict[str, Any]) -> None:
    """The ``trial-metrics`` shape of interfaces.md §2.10 (RuntimeFault TRIAL_METRICS)."""
    errors = sorted(
        Draft202012Validator(RECORD_SCHEMA).iter_errors(value), key=lambda e: list(e.path)
    )
    if errors:
        raise RuntimeFault(
            "TRIAL_METRICS",
            "trial-metrics record does not match §2.10",
            details=[f"{'/'.join(map(str, e.path))}: {e.message}" for e in errors[:5]],
        )


def _per_solved(values: list[float], solved: int) -> float | None:
    """Everything the arm spent, over the tasks it solved (failures still cost)."""
    return round(sum(values) / solved, 1) if solved and values else None


class TrialMetrics:
    def __init__(
        self, service: LocalExecutionService, tables: list[pricing.PriceTable] | None = None
    ) -> None:
        self.service, self.store, self.scope = service, service.store, service.scope
        self.artifacts = service.workspaces.artifacts
        self.tables = pricing.load_tables() if tables is None else tables
        self._sampling: dict[str, tuple[str | None, str | None]] = {}

    # -- one trial -----------------------------------------------------------------------------
    def trial(self, trial: dict[str, Any]) -> dict[str, Any]:
        """The D-094 facts of one trial (existing keys) and the §2.10 fields."""
        refs = trial.get("artifact_refs") or []
        # an executor fault leaves no receipt (evaluation/service.py: artifact_refs ()): no goal
        receipt: dict[str, Any] = (
            json.loads(self.artifacts.read(self.scope, refs[0], trusted=True)) if refs else {}
        )
        goal_id = receipt.get("goal_id")
        calibration = bool(trial.get("calibration_plan_ref"))
        elapsed = trial.get("elapsed_ms")
        facts: dict[str, Any] = {
            "task_id": trial["task_id"],
            "arm": trial.get("arm") or (CALIBRATION_ARM if calibration else None),
            "success": trial["success"],
            "goal_status": receipt.get("goal_status"),
            "hidden_passed": receipt.get("hidden_passed"),
            "seconds": round(elapsed / 1000, 1) if elapsed is not None else None,
            "input_tokens": trial.get("input_tokens"),
            "output_tokens": trial.get("output_tokens"),
        }
        facts["verified_hidden_fail"] = (
            receipt.get("goal_status") == "verified" and receipt.get("hidden_passed") is False
        )
        split, stage = self._split_stage(trial, receipt)
        cell = receipt.get("cell_id") or trial.get("cell_id")
        planner = receipt.get("planner") or {}
        real_planner = planner.get("mode") == "real"
        v2: dict[str, Any] = {
            "experiment_ref": trial.get("experiment_ref"),
            "calibration_plan_ref": trial.get("calibration_plan_ref"),
            "source": "calibration" if calibration else "experiment",
            "split": split,
            "stage": stage,
            "domain": trial.get("task_class"),
            "cell_id": cell,
            "strategy": receipt.get("strategy"),
            "wall_seconds": round(elapsed / 1000, 3) if elapsed is not None else None,
            "cells_used": [cell] if cell else [],
            # ``turns``: the executor turns AMPLAI dispatched (first turns and follow-ups, every
            # revision; ``strategy_metrics``). Provider-internal turns (Claude ``num_turns``) are
            # not kept in the run records, so ``provider_turns`` stays null until §14 Q14.
            "turns": 0,
            "provider_turns": None,
            # no goal ran: no strategy mechanism ran either
            "escalations": 0,
            "reviewer_rounds": 0,
            "fix_requests": 0,
            "best_of_n_first_pass": None,
            "sub_agents": 0,
            "integration_conflicts": 0,
            "re_verifications": 0,
            "candidates": 0,
            "candidate_turns": 0,
            "vote_selected": None,
            "phase": "calibration" if calibration else "stage",
            "fidelity": None,
        }
        ask_back = bool(planner.get("questions")) and trial.get("task_class") != "ambiguity"
        if not goal_id:
            return {
                **facts, "diff": None, "api_cost": {"status": "no_goal"}, **v2,
                "tokens": {"input": facts["input_tokens"], "output": facts["output_tokens"],
                           "cached_input": None, "reasoning": None},
                "verification_seconds": None, "agent_calls": 1 if real_planner else 0,
                "attempts_used": 0, "nodes": 0,
                "guards": self._guards(ask_back, None, facts["verified_hidden_fail"]),
            }  # fmt: skip
        plan = self.service.plan_record(goal_id)
        last = plan.get("attempts") or []  # the last revision's attempts (its change is graded)
        # every revision's attempts: an escalated trial (M6, Work 033 S9) ran earlier revisions
        attempts = [*(plan.get("previous_attempts") or []), *last]
        change = next((a["change"] for a in reversed(last) if a.get("change")), None)
        strategy = self._strategy_metrics(plan)
        facts["attempts"] = len(attempts)
        facts["diff"] = self._diff(change) if change else None
        facts["api_cost"] = self._cost(plan, attempts, planner if real_planner else None)
        run_ids = goal_run_ids(self.store, self.scope, plan)
        runs = self._runs(run_ids)
        executions = run_executions(self.store, self.scope, run_ids)
        started = sum(1 for e in executions if e["data"].get("driver_handle"))
        graph_nodes = (
            len(self.store.get(self.scope, "workgraph", plan["graph_ref"])["nodes"])
            if plan.get("graph_ref")
            else 0
        )
        composition_ref = trial.get("composition_ref")
        if composition_ref and not v2["cell_id"] and plan.get("app"):
            # a receipt from before S8 names no cell: the trial composition's installed cell
            cell = cell_of(self.service, plan["app"], composition_ref)
            v2.update(cell_id=cell, cells_used=[cell] if cell else [])
        if composition_ref and not v2["strategy"]:
            v2["strategy"] = strategy_of(self.service, composition_ref)
        # what the strategy did, over every revision (plan.md §8.1; S9 ``strategy_metrics``)
        v2.update({k: strategy[k] for k in STRATEGY_FIELDS if k not in VOTE_FIELDS})
        v2.update(_vote_fields(strategy))
        cells = [str(c) for c in strategy.get("cells_used") or [] if c]
        if cells:
            v2["cells_used"] = sorted({*cells, *v2["cells_used"]})
        return {
            **facts,
            **v2,
            "tokens": self._tokens(
                facts, attempts, runs, planner if real_planner else None, plan.get("aux_usage")
            ),
            "verification_seconds": self._verification_seconds(runs),
            # processes started for the goal's dispatches (every revision), the follow-up turns
            # resumed in them (M3), the vote candidates beyond each attempt's own turn (M4: a
            # candidate ``<dispatch_id>-c<i>`` is its own process with no execution row, S9b;
            # ``strategy_metrics.candidate_turns``), the auxiliary read-only turns that ran
            # (M2/M7, a turn that never started excluded) and the real planner's turn
            "agent_calls": started
            + int(strategy.get("followups") or 0)
            + int(strategy.get("candidate_turns") or 0)
            + int(strategy.get("aux_turns") or 0)
            + (1 if real_planner else 0),
            "attempts_used": len(attempts),
            "nodes": graph_nodes,
            "guards": self._guards(ask_back, facts["diff"], facts["verified_hidden_fail"]),
        }

    def _strategy_metrics(self, plan: dict[str, Any]) -> dict[str, Any]:
        """The plan record's ``strategy_metrics`` (written by the loop when the goal finished,
        Work 033 S9), else the same computation over the stored plan (a goal the loop never
        finished, e.g. one closed at plan time, or a record from before S9)."""
        stored = plan.get("strategy_metrics")
        attempts = [*(plan.get("previous_attempts") or []), *(plan.get("attempts") or [])]
        if (
            isinstance(stored, dict)
            and all(k in stored for k in STRATEGY_FIELDS)
            # written for this revision: an escalation after the loop finished a revision, or a
            # revision the executor closed without the loop finishing it, leaves it behind
            and stored.get("escalations") == len(plan.get("escalations") or [])
            and stored.get("attempts_used") == len(attempts)
        ):
            return stored
        computed: dict[str, Any] = self.service.strategy_runner().metrics(plan)
        return computed

    @staticmethod
    def _guards(ask_back: bool, diff: dict[str, Any] | None, vhf: bool) -> dict[str, Any]:
        """Per-trial hack-guard signals (§9.10); the last change's diff is the edit."""
        return {
            "ask_back": ask_back,
            "edit_files": diff["files"] if diff else 0,
            "edit_lines": diff["lines_added"] + diff["lines_removed"] if diff else 0,
            # null until the Codex and Claude tool-call stream names are verified (§14 Q14)
            "broken_tool_calls": None,
            "test_file_edits": len(diff["tests_changed"]) if diff else 0,
            "verified_hidden_fail": vhf,
        }

    def _split_stage(
        self, trial: dict[str, Any], receipt: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        """The case's split (the trial's, the receipt's, else the experiment's sampling plan)
        and the stage of the experiment's sampling plan (null for calibration)."""
        experiment_ref = trial.get("experiment_ref")
        split = trial.get("split") or receipt.get("split")
        if not experiment_ref:
            return split, None
        key = json.dumps(experiment_ref, sort_keys=True)
        if key not in self._sampling:
            plan = self.store.get(self.scope, "eval-experiment", experiment_ref)
            _, sampling = resolve_ref(self.store, self.scope, plan["sampling_plan_ref"])
            self._sampling[key] = (sampling.get("split"), sampling.get("stage"))
        sampled_split, stage = self._sampling[key]
        return split or sampled_split, stage

    def _runs(self, run_ids: list[str]) -> dict[str, dict[str, Any]]:
        runs = {}
        for run_id in run_ids:
            try:
                runs[run_id] = self.store.head(self.scope, "run", run_id)["data"]["record"]
            except RuntimeFault:
                continue
        return runs

    def _detail(self, usage: dict[str, Any]) -> dict[str, Any] | None:
        ref = usage.get("source_ref")
        if ref and ref["id"].startswith(USAGE_DETAIL_KIND + "-"):
            value: dict[str, Any] = self.store.get(self.scope, USAGE_DETAIL_KIND, ref)
            return value
        return None

    def _tokens(
        self,
        facts: dict[str, Any],
        attempts: list[dict[str, Any]],
        runs: dict[str, dict[str, Any]],
        planner: dict[str, Any] | None,
        aux: list[dict[str, Any]] | None = None,
    ) -> dict[str, int | None]:
        """Input and output as the trial counted them; cached input and reasoning summed over
        the attempts' runs, the real planner's turn and the auxiliary read-only turns that ran
        (``aux_usage``, Work 033 S9) when every part reports them."""
        cached: int | None = 0
        reasoning: int | None = 0
        parts = 0
        turns = [{"usage": (planner or {}).get("usage")}] if planner is not None else []
        turns += self._aux_ran({"aux_usage": aux})  # a turn that never started spent nothing
        for attempt in attempts:
            run = runs.get(attempt.get("run_id") or "")
            detail = self._detail((run or {}).get("usage") or {}) if run else None
            parts += 1
            fields = (detail or {}).get("fields") or {}
            provider = (detail or {}).get("provider")
            c = fields.get(CACHED_FIELD.get(str(provider), ""))
            r = fields.get(REASONING_FIELD.get(str(provider), ""))
            cached = cached + c if cached is not None and type(c) is int else None
            reasoning = reasoning + r if reasoning is not None and type(r) is int else None
        for turn in turns:
            reported = turn.get("usage")
            usage: dict[str, Any] = reported if isinstance(reported, dict) else {}
            parts += 1
            # a read-only turn (planner, reviewer, investigator, lead) reports the provider's own
            # usage keys (readonly_turn.py)
            c = next((usage[k] for k in CACHED_FIELD.values() if type(usage.get(k)) is int), None)
            r = next(
                (usage[k] for k in REASONING_FIELD.values() if type(usage.get(k)) is int), None
            )
            cached = cached + c if cached is not None and c is not None else None
            reasoning = reasoning + r if reasoning is not None and r is not None else None
        if not parts:
            cached = reasoning = None
        return {
            "input": facts["input_tokens"],
            "output": facts["output_tokens"],
            "cached_input": cached,
            "reasoning": reasoning,
        }

    def _verification_seconds(self, runs: dict[str, dict[str, Any]]) -> float | None:
        """Evidence time over the goal's runs (``started_at`` to ``finished_at`` of each piece of
        verification evidence); null when no run was verified."""
        total, seen = 0.0, False
        for record in runs.values():
            for ref in record.get("evidence_refs") or []:
                try:
                    evidence = self.store.get(self.scope, "evidence", ref)
                    total += max(0.0, _utc(evidence["finished_at"]) - _utc(evidence["started_at"]))
                    seen = True
                except (RuntimeFault, Hold, KeyError, ValueError):
                    continue
        return round(total, 3) if seen else None

    def _diff(self, change: dict[str, Any]) -> dict[str, Any]:
        value = json.loads(self.artifacts.read(self.scope, change))
        return diff_stats(self.artifacts.read(self.scope, value["patch"]))

    def _cost(
        self,
        plan: dict[str, Any],
        attempts: list[dict[str, Any]],
        planner: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        model = (plan.get("composition") or {}).get("model")
        if plan.get("previous_attempts"):
            # an earlier revision ran (an escalation, M6, Work 033 S9, or a replan) and may have
            # run on another cell; the plan record keeps only the last revision's model, so
            # pricing every run at it could misprice them, and pricing the last revision alone
            # would leave runs out
            return {"status": "revisions_unpriced", "model": model}
        if any(e.get("tokens") is None for e in self._aux_ran(plan)):
            # an auxiliary turn may have spent tokens nobody counted: a run-only figure would look
            # complete and is not
            return {"status": "aux_unknown_usage", "model": model}
        day = str(plan.get("approved_at") or "")[:10] or datetime.now().date().isoformat()
        total, notes, upper, statuses = 0, set(), False, set()
        priced: list[tuple[str, dict[str, Any] | None, dict[str, Any]]] = []
        for attempt in attempts:
            run = self.store.head(self.scope, "run", attempt["run_id"])["data"]["record"]
            usage = run.get("usage") or {}
            priced.append((str(model), self._detail(usage), usage))
        if planner is not None:
            # the real planner's turn (§8.3), at the planning composition's model; its usage has
            # totals only, so it is priced as an upper bound
            planned_model = (plan.get("planned_with") or {}).get("model") or model
            priced.append((str(planned_model), None, planner.get("usage") or {}))
        for entry in self._aux_ran(plan):
            # an auxiliary read-only turn (§5.3 ``aux_usage``, Work 033 S9), at its cell's model;
            # like the planner's it reports totals only, so it is priced as an upper bound
            aux_model = self._cell_model(plan, entry.get("cell_id"))
            if aux_model is None:
                return {"status": "aux_unpriced", "model": model}
            priced.append((aux_model, None, entry.get("usage") or {}))
        for one_model, detail, usage in priced:
            one = pricing.estimate(one_model, day, detail=detail, usage=usage, tables=self.tables)
            statuses.add(one["status"])
            if one["status"] != "estimated":
                return {"status": one["status"], "model": model}
            total += one["cost_microunits"]
            notes.update(one["notes"])
            upper = upper or one["upper_bound"]
        if not priced:
            return {"status": "no_usage", "model": model}
        return {
            "status": "estimated",
            "cost_microunits": total,
            "currency": "USD",
            "model": model,
            "price_table_id": pricing.table_for(self.tables, day).table_id,  # type: ignore[union-attr]
            "notes": sorted(notes),
            "upper_bound": upper,
        }

    @staticmethod
    def _aux_ran(plan: dict[str, Any]) -> list[dict[str, Any]]:
        """The plan's auxiliary read-only turns that started (``aux_usage``); a turn that never
        started (``tokens`` 0, no usage; ``strategy_runner.AuxLedger``) spent nothing."""
        return [
            e for e in plan.get("aux_usage") or []
            if isinstance(e, dict) and not (e.get("tokens") == 0 and e.get("usage") is None)
        ]  # fmt: skip

    def _cell_model(self, plan: dict[str, Any], cell_id: Any) -> str | None:
        """The provider model of an installed cell (its ``harness-cell`` record); the goal's own
        cell is the plan's composition model. None when the cell cannot be resolved."""
        from ..runtime.execution.cells import CELL_KIND, CellInstaller

        if not isinstance(cell_id, str) or not cell_id:
            return None
        composition = plan.get("composition") or {}
        if cell_id == composition.get("cell_id") and composition.get("model"):
            return str(composition["model"])
        try:
            ref = CellInstaller(self.store, self.scope).registry()["cells"].get(cell_id)
            if ref is None:
                return None
            model = self.store.get(self.scope, CELL_KIND, ref).get("provider_model_id")
        except (Hold, RuntimeFault, KeyError, TypeError):
            return None
        return str(model) if model else None

    # -- the stored record ---------------------------------------------------------------------
    def record(self, trial_ref: dict[str, Any]) -> dict[str, Any]:
        """Write the ``trial-metrics`` record of one stored trial (id ``tm-<trial_id>``; a new
        store revision each time it is recomputed)."""
        kind, trial = resolve_ref(self.store, self.scope, trial_ref)
        if kind not in TRIAL_KINDS:
            raise Hold(
                "TRIAL_METRICS_SOURCE", "Not an experiment or calibration trial", details=kind
            )
        row = self.trial(trial)
        value = {
            "schema": SCHEMA,
            "scope": self.scope.wire(),
            "trial_ref": {k: trial_ref[k] for k in ("id", "revision", "digest")},
            **{k: row[k] for k in RECORD_SCHEMA["required"] if k in row},
            **{k: row[k] for k in VOTE_FIELDS if k in row},
            "task_id": trial["task_id"],
            "domain": row["domain"] or "",
            "cell_id": row["cell_id"] or "",
            "strategy": row["strategy"] or "",
            "wall_seconds": row["wall_seconds"] if row["wall_seconds"] is not None else 0.0,
            "computed_at": now(),
        }
        validate_record(value)
        record_id = "tm-" + str(trial.get("trial_id") or trial_ref["id"])
        with self.store.tx() as db:
            latest = db.execute(
                "SELECT MAX(revision) FROM objects WHERE tenant=? AND project=? AND kind=? "
                "AND id=?",
                (*self.scope.keys(), KIND, record_id),
            ).fetchone()[0]
            ref: dict[str, Any] = self.store.put(
                db, self.scope, KIND, record_id, (latest or 0) + 1, value
            )
        return ref

    # -- per arm -------------------------------------------------------------------------------
    @staticmethod
    def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for arm in sorted({str(r["arm"]) for r in rows}):
            items = [r for r in rows if str(r["arm"]) == arm]
            out[arm] = {
                **TrialMetrics._summary(items),
                # §3.10: per strategy inside the arm (rows before S8 have none recorded)
                "strategies": {
                    strategy: TrialMetrics._brief(
                        [r for r in items if (r.get("strategy") or "unrecorded") == strategy]
                    )
                    for strategy in sorted({r.get("strategy") or "unrecorded" for r in items})
                },
            }
        return out

    @staticmethod
    def _brief(items: list[dict[str, Any]]) -> dict[str, Any]:
        solved = [r for r in items if r["success"] is True]
        return {
            "trials": len(items),
            "solved": len(solved),
            "unknown": sum(r["success"] is None for r in items),
            "tokens_per_solved": _per_solved(
                [(r["input_tokens"] or 0) + (r["output_tokens"] or 0) for r in items],
                len(solved),
            ),
            "seconds_per_solved": _per_solved(
                [r["seconds"] for r in items if r.get("seconds") is not None], len(solved)
            ),
        }

    @staticmethod
    def _summary(items: list[dict[str, Any]]) -> dict[str, Any]:
        solved = [r for r in items if r["success"] is True]
        costs = [r["api_cost"] for r in items if r["api_cost"].get("status") == "estimated"]
        diffs = [r["diff"] for r in items if r.get("diff")]
        seconds = [r["seconds"] for r in items if r.get("seconds") is not None]
        return {
            "trials": len(items),
            "solved": len(solved),
            "unknown": sum(r["success"] is None for r in items),
            "verified_hidden_fail": sum(r["verified_hidden_fail"] for r in items),
            "tests_added": sum(bool(d["tests_added"]) for d in diffs),
            "tests_changed": sum(bool(d["tests_changed"]) for d in diffs),
            "tokens_per_solved": _per_solved(
                [(r["input_tokens"] or 0) + (r["output_tokens"] or 0) for r in items],
                len(solved),
            ),
            "seconds_per_solved": _per_solved(seconds, len(solved)),
            "seconds_median": median(seconds) if seconds else None,
            "api_cost_usd_per_solved": (
                round(sum(c["cost_microunits"] for c in costs) / len(solved) / 1e6, 4)
                if solved and len(costs) == len(items)
                else None
            ),
            "api_cost_upper_bound": any(c.get("upper_bound") for c in costs),
            "api_cost_priced": f"{len(costs)}/{len(items)}",
            "diff_files_mean": round(mean(d["files"] for d in diffs), 2) if diffs else None,
            "diff_lines_mean": (
                round(mean(d["lines_added"] + d["lines_removed"] for d in diffs), 1)
                if diffs
                else None
            ),
        }

    # -- hack guards (§9.10) ---------------------------------------------------------------------
    @staticmethod
    def guards(
        baseline: list[dict[str, Any]],
        candidate: list[dict[str, Any]],
        thresholds: dict[str, float],
    ) -> list[str]:
        """Hack-guard findings of a screening comparison (§2.9 thresholds, §9.10).

        Rows are ``trial-metrics`` rows (``guards`` per trial). A signal whose shift from the
        baseline reaches its threshold is a finding; a finding fails screening (HACK_GUARD) and
        never adds to a score. ``broken_tool_calls_rate`` is skipped while any row's metric is
        null (§9.10); ``edit_rate_ratio`` is skipped when the baseline edits nothing (no ratio).
        """
        findings: list[str] = []

        def rows_guards(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [r["guards"] for r in rows if isinstance(r.get("guards"), dict)]

        limit = thresholds.get("verified_hidden_fail")
        if limit is not None:
            b = len(
                {
                    r["task_id"]
                    for r in baseline
                    if (r.get("guards") or {}).get("verified_hidden_fail")
                }
            )
            c = len({r["task_id"] for r in candidate if (r.get("guards") or {}).get(
                "verified_hidden_fail")})  # fmt: skip
            if c - b >= limit:
                findings.append(
                    f"verified_hidden_fail: +{c - b} tasks (baseline {b}, candidate {c}; "
                    f"threshold +{limit:g})"
                )
        limit = thresholds.get("ask_back_rate")
        if limit is not None:

            def ask_rate(rows: list[dict[str, Any]]) -> float | None:
                # ask-back counts on non-ambiguity tasks only (§9.10)
                eligible = [r for r in rows if r.get("domain") != "ambiguity" and r.get("guards")]
                if not eligible:
                    return None
                return sum(bool(r["guards"]["ask_back"]) for r in eligible) / len(eligible)

            b_rate, c_rate = ask_rate(baseline), ask_rate(candidate)
            if b_rate is not None and c_rate is not None and c_rate - b_rate >= limit:
                findings.append(
                    f"ask_back_rate: +{c_rate - b_rate:.3f} (baseline {b_rate:.3f}, "
                    f"candidate {c_rate:.3f}; threshold +{limit:g})"
                )
        limit = thresholds.get("broken_tool_calls_rate")
        if limit is not None:
            b_rows, c_rows = rows_guards(baseline), rows_guards(candidate)
            counted = [g.get("broken_tool_calls") for g in (*b_rows, *c_rows)]
            if b_rows and c_rows and all(type(x) is int for x in counted):
                # (추정) the rate is the share of trials with at least one broken call; §9.10
                # does not define it and the metric is null until §14 Q14 is answered
                b_rate = sum(g["broken_tool_calls"] > 0 for g in b_rows) / len(b_rows)
                c_rate = sum(g["broken_tool_calls"] > 0 for g in c_rows) / len(c_rows)
                if c_rate - b_rate >= limit:
                    findings.append(
                        f"broken_tool_calls_rate: +{c_rate - b_rate:.3f} (baseline "
                        f"{b_rate:.3f}, candidate {c_rate:.3f}; threshold +{limit:g})"
                    )
        limit = thresholds.get("edit_rate_ratio")
        if limit is not None:
            b_rows, c_rows = rows_guards(baseline), rows_guards(candidate)
            if b_rows and c_rows:
                b_mean = mean(g["edit_lines"] for g in b_rows)
                c_mean = mean(g["edit_lines"] for g in c_rows)
                if b_mean > 0 and c_mean / b_mean >= limit:
                    findings.append(
                        f"edit_rate_ratio: x{c_mean / b_mean:.2f} mean edit lines (baseline "
                        f"{b_mean:.1f}, candidate {c_mean:.1f}; threshold x{limit:g})"
                    )
        return findings
