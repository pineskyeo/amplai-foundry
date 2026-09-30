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
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import PurePosixPath
from statistics import mean, median
from typing import Any

from ..evaluation import pricing
from ..runtime.execution.product import LocalExecutionService
from ..runtime.execution.worker import USAGE_DETAIL_KIND

DIFF_FILE = re.compile(r"^diff --git a/(\S+) b/(\S+)$")


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

    # -- one trial -----------------------------------------------------------------------------
    def trial(self, trial: dict[str, Any]) -> dict[str, Any]:
        receipt = json.loads(
            self.artifacts.read(self.scope, trial["artifact_refs"][0], trusted=True)
        )
        goal_id = receipt.get("goal_id")
        facts: dict[str, Any] = {
            "task_id": trial["task_id"],
            "arm": trial["arm"],
            "success": trial["success"],
            "goal_status": receipt.get("goal_status"),
            "hidden_passed": receipt.get("hidden_passed"),
            "seconds": round(trial["elapsed_ms"] / 1000, 1),
            "input_tokens": trial.get("input_tokens"),
            "output_tokens": trial.get("output_tokens"),
        }
        facts["verified_hidden_fail"] = (
            receipt.get("goal_status") == "verified" and receipt.get("hidden_passed") is False
        )
        if not goal_id:
            return {**facts, "diff": None, "api_cost": {"status": "no_goal"}}
        plan = self.service.plan_record(goal_id)
        attempts = plan.get("attempts") or []
        change = next((a["change"] for a in reversed(attempts) if a.get("change")), None)
        facts["attempts"] = len(attempts)
        facts["diff"] = self._diff(change) if change else None
        facts["api_cost"] = self._cost(plan, attempts)
        return facts

    def _diff(self, change: dict[str, Any]) -> dict[str, Any]:
        value = json.loads(self.artifacts.read(self.scope, change))
        return diff_stats(self.artifacts.read(self.scope, value["patch"]))

    def _cost(self, plan: dict[str, Any], attempts: list[dict[str, Any]]) -> dict[str, Any]:
        model = (plan.get("composition") or {}).get("model")
        day = str(plan.get("approved_at") or "")[:10] or datetime.now().date().isoformat()
        total, notes, upper, statuses = 0, set(), False, set()
        for attempt in attempts:
            run = self.store.head(self.scope, "run", attempt["run_id"])["data"]["record"]
            usage = run.get("usage") or {}
            detail = None
            ref = usage.get("source_ref")
            if ref and ref["id"].startswith(USAGE_DETAIL_KIND + "-"):
                detail = self.store.get(self.scope, USAGE_DETAIL_KIND, ref)
            one = pricing.estimate(str(model), day, detail=detail, usage=usage, tables=self.tables)
            statuses.add(one["status"])
            if one["status"] != "estimated":
                return {"status": one["status"], "model": model}
            total += one["cost_microunits"]
            notes.update(one["notes"])
            upper = upper or one["upper_bound"]
        if not attempts:
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

    # -- per arm -------------------------------------------------------------------------------
    @staticmethod
    def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for arm in sorted({r["arm"] for r in rows}):
            items = [r for r in rows if r["arm"] == arm]
            solved = [r for r in items if r["success"] is True]
            costs = [r["api_cost"] for r in items if r["api_cost"].get("status") == "estimated"]
            diffs = [r["diff"] for r in items if r.get("diff")]

            out[arm] = {
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
                "seconds_per_solved": _per_solved([r["seconds"] for r in items], len(solved)),
                "seconds_median": median(r["seconds"] for r in items) if items else None,
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
        return out
