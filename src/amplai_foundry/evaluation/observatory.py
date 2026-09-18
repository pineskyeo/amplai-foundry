"""Scoped, audit-derived observations. Missing data never becomes a zero or a pass.

RunRecord is authoritative for execution; metrics are a read-only projection. Goal
rates use goals, not retries, as the denominator. No prompts or hidden reasoning
are exported. Wall elapsed time is not labelled compute time.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from statistics import median
from typing import Any

from amplai_foundry.runtime.errors import RuntimeFault

TERMINAL_GOALS = frozenset(
    {"verified", "failed", "cancelled", "blocked", "aborted", "inconclusive"}
)
FILTERS = frozenset({"composition", "model", "driver", "task_class", "risk", "repo"})


def _instant(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("timezone required")
        return dt.timestamp()
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeFault("METRIC_TIME", "Use an ISO-8601 timestamp with a timezone") from exc


class Observatory:
    def __init__(self, store):
        self.store = store

    def summary(
        self,
        scope,
        *,
        filters: dict | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> dict:
        filters = filters or {}
        if set(filters) - FILTERS or any(not isinstance(v, str) or not v for v in filters.values()):
            raise RuntimeFault("METRIC_FILTER", "Unknown or empty metric slice")
        lo, hi = _instant(since), _instant(until)
        if lo is not None and hi is not None and lo >= hi:
            raise RuntimeFault("METRIC_WINDOW", "The time window must be nonempty [since, until)")
        # One local owner serializes mutations. Hold its lock over the whole snapshot.
        with self.store._lock:
            heads = self.store.conn.execute(
                "SELECT kind,id,state,data FROM heads WHERE tenant=? AND project=? "
                "AND kind IN ('run','goal','effect')",
                scope.keys(),
            ).fetchall()
            objects = self.store.conn.execute(
                "SELECT kind,id,revision,digest,data FROM objects WHERE tenant=? AND project=? "
                "AND kind IN ('goal-contract','workgraph','app-binding','verdict','goal-verification')",
                scope.keys(),
            ).fetchall()
            events = self.store.conn.execute(
                "SELECT seq,event_type,aggregate_id,created_at FROM events WHERE tenant=? AND project=? "
                "ORDER BY seq",
                scope.keys(),
            ).fetchall()
        obj = {
            (r["kind"], r["id"], r["revision"], r["digest"]): json.loads(r["data"]) for r in objects
        }

        def get(kind, ref):
            if not isinstance(ref, dict):
                return None
            return obj.get((kind, ref.get("id"), ref.get("revision"), ref.get("digest")))

        decoded = [{**dict(r), "data": json.loads(r["data"])} for r in heads]
        runs = [r["data"]["record"] for r in decoded if r["kind"] == "run"]
        selected, dimensions, integrity = [], {}, []
        for r in runs:
            contract = get("goal-contract", r.get("contract_ref"))
            graph = get("workgraph", r.get("graph_ref"))
            node = next(
                (n for n in (graph or {}).get("nodes", []) if n["work_id"] == r["work_id"]), {}
            )
            binding = get("app-binding", node.get("target_ref"))
            labels = {
                "composition": r["composition_ref"]["digest"],
                "model": r["model_profile_ref"]["digest"],
                "driver": r["driver_profile_ref"]["digest"],
                "task_class": node.get("task_class", "unreported"),
                "risk": (contract or {}).get(
                    "risk_class", (contract or {}).get("risk", "unreported")
                ),
                "repo": (binding or {}).get("repo_identity", "unreported"),
            }
            if not isinstance(labels["risk"], str):
                labels["risk"] = str(labels["risk"].get("level", "unreported"))
            started = _instant(r["started_at"])
            if (lo is not None and started < lo) or (hi is not None and started >= hi):
                continue
            if any(labels[k] != v for k, v in filters.items()):
                continue
            if not contract or not graph:
                integrity.append({"run_id": r["run_id"], "finding": "missing_contract_or_graph"})
            selected.append(r)
            dimensions[r["run_id"]] = labels
        ids = {r["root_goal_id"] for r in selected}
        # A sliced goal is represented once even if several runs matched the slice.
        goals = [
            h
            for h in decoded
            if h["kind"] == "goal"
            and (h["id"] in ids or (not filters and lo is None and hi is None))
        ]
        goal_counts = Counter(h["state"] for h in goals)
        eligible = sum(goal_counts[s] for s in ("verified", "failed"))
        valid_goals = []
        for h in goals:
            proof = get("goal-verification", h["data"].get("goal_verification_ref"))
            if h["state"] == "verified":
                if (
                    proof
                    and proof.get("outcome") == "pass"
                    and proof.get("attestation_ref")
                    and proof.get("contract_ref") == h["data"].get("active_contract_ref")
                    and proof.get("graph_ref") == h["data"].get("active_graph_ref")
                ):
                    valid_goals.append(h)
                else:
                    integrity.append(
                        {"goal_id": h["id"], "finding": "missing_current_global_verification"}
                    )
        verified = len(valid_goals)
        first_attempt = sum(
            bool([r for r in runs if r["root_goal_id"] == h["id"]])
            and all(r["attempt"] == 1 for r in runs if r["root_goal_id"] == h["id"])
            for h in valid_goals
        )
        currencies: dict[str, dict[str, Any]] = {}
        for r in selected:
            usage = r["usage"]
            item = currencies.setdefault(
                usage["currency"],
                {
                    "measured_microunits": 0,
                    "estimated_microunits": 0,
                    "measured_runs": 0,
                    "estimated_runs": 0,
                    "unknown_runs": 0,
                },
            )
            status, cost = usage["status"], usage["cost_microunits"]
            if status in {"measured", "estimated"} and type(cost) is int and cost >= 0:
                item[status + "_microunits"] += cost
                item[status + "_runs"] += 1
            else:
                item["unknown_runs"] += 1
        for item in currencies.values():
            item["total_cost_microunits"] = (
                item["measured_microunits"]
                if item["unknown_runs"] == 0 and item["estimated_runs"] == 0
                else None
            )
        cost_values = list(currencies.values())
        known_cost = sum(x["measured_microunits"] + x["estimated_microunits"] for x in cost_values)
        elapsed = []
        for r in selected:
            if r["finished_at"]:
                delta = _instant(r["finished_at"]) - _instant(r["started_at"])
                if delta >= 0:
                    elapsed.append(delta * 1000)
                else:
                    integrity.append({"run_id": r["run_id"], "finding": "negative_wall_duration"})
        coverage_total, coverage_evidence, coverage_pass, coverage_waived = 0, 0, 0, 0
        for h in goals:
            cref = h["data"].get("active_contract_ref")
            contract = get("goal-contract", cref)
            if not contract:
                continue
            mandatory = [a for a in contract.get("acceptance", []) if a.get("mandatory", True)]
            for criterion in mandatory:
                coverage_total += 1
                outcomes = []
                for r in runs:
                    if r["root_goal_id"] != h["id"] or r["contract_ref"] != cref:
                        continue
                    for ref in r["verdict_refs"]:
                        v = get("verdict", ref)
                        if (
                            v
                            and v.get("acceptance_id") == criterion["id"]
                            and v.get("attestation_ref")
                        ):
                            outcomes.append(v["outcome"])
                if outcomes:
                    coverage_evidence += 1
                if "pass" in outcomes:
                    coverage_pass += 1
                if "waived" in outcomes:
                    coverage_waived += 1
        grouped = {}
        for dim in sorted(FILTERS):
            grouped[dim] = dict(Counter(dimensions[r["run_id"]][dim] for r in selected))
        scoped_events = [
            e
            for e in events
            if e["aggregate_id"] in ids or e["aggregate_id"] in {r["run_id"] for r in selected}
        ]
        event_counts = Counter(e["event_type"] for e in scoped_events)
        return {
            "scope": scope.wire(),
            "snapshot_event_seq": events[-1]["seq"] if events else 0,
            "window": {"since": since, "until": until, "run_basis": "started_at"},
            "filters": filters,
            "run_count": len(selected),
            "status_counts": dict(Counter(r["status"] for r in selected)),
            "goal_count": len(goals),
            "goal_status_counts": dict(goal_counts),
            "eligible_terminated_goals": eligible,
            "verified_goals": verified,
            "verified_goal_rate": verified / eligible if eligible else None,
            "first_attempt_verified_goals": first_attempt,
            "first_attempt_verified_rate": first_attempt / eligible if eligible else None,
            "excluded_goals": dict(
                (s, n) for s, n in goal_counts.items() if s not in {"verified", "failed"}
            ),
            "acceptance": {
                "mandatory": coverage_total,
                "with_attested_verdict": coverage_evidence,
                "with_pass": coverage_pass,
                "waived": coverage_waived,
                "coverage": coverage_evidence / coverage_total if coverage_total else None,
                "basis": "current-contract immutable server-attested verdict references",
            },
            "cost_by_currency": currencies,
            "known_cost_sum_microunits": known_cost if len(currencies) <= 1 else None,
            "unknown_cost_run_count": sum(x["unknown_runs"] for x in cost_values),
            "total_cost_microunits": cost_values[0]["total_cost_microunits"]
            if len(cost_values) == 1
            else None,
            "composition_samples": grouped["composition"],
            "slices": grouped,
            "strategy_samples": dict(
                Counter(
                    next(
                        (
                            n.get("strategy", "unreported")
                            for n in (get("workgraph", r["graph_ref"]) or {}).get("nodes", [])
                            if n["work_id"] == r["work_id"]
                        ),
                        "unreported",
                    )
                    for r in selected
                )
            ),
            "failure_signatures": dict(
                Counter(s for r in selected for s in r["failure_signatures"])
            ),
            "repair_runs": sum(r["attempt"] > 1 for r in selected),
            "human_intervention_events": {
                k: v
                for k, v in event_counts.items()
                if k.startswith(("question.", "steering.", "approval."))
            },
            "event_counts": dict(event_counts),
            "run_wall_elapsed_ms": {
                "samples": len(elapsed),
                "median": median(elapsed) if elapsed else None,
            },
            "compute_ms": None,
            "queue_ms": None,
            "human_wait_ms": None,
            "integrity_findings": integrity,
            "note": "Descriptive counts, not causal effects. Unknown time breakdowns are not inferred. "
            "Small slices expose counts; a zero incident count is not proof of zero risk.",
        }

    def export_batch(self, scope, after: int, exporter, *, limit: int = 100):
        from .telemetry import project_event

        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise RuntimeFault("TELEMETRY_RANGE", "Invalid cursor or batch limit")
        events = self.store.events(scope, after=after, limit=limit)
        safe = [project_event(scope, event) for event in events]
        if safe:
            self.store.assert_outside_tx()
            exporter(safe)
        return events[-1]["seq"] if events else after
