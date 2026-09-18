"""Root-goal reservations. Unknown usage is never treated as free usage."""

from __future__ import annotations

import json

from ..errors import Hold, RuntimeFault
from ..storage.store import Scope, Store


class BudgetService:
    def __init__(self, store: Store):
        self.store = store

    def reserve(
        self,
        db,
        scope: Scope,
        goal_id: str,
        run_id: str,
        root: dict,
        request: dict,
        *,
        depth: int = 0,
        attempt: int = 1,
        goal_started: float | None = None,
    ) -> None:
        if depth > root["max_delegation_depth"] or depth > request["max_delegation_depth"]:
            raise Hold("DELEGATION_DEPTH", "Native and AMPLAI child depth exceed the root ceiling")
        if attempt > min(root["max_attempts"], request["max_attempts"]):
            raise Hold(
                "ATTEMPT_BUDGET", "Initial execution is attempt one; repair budget exhausted"
            )
        if request["currency"] != root["currency"]:
            raise Hold("BUDGET_CURRENCY", "Different currencies cannot be combined")
        if (
            goal_started is not None
            and self.store.clock() - goal_started >= root["max_wall_seconds"]
        ):
            raise Hold("WALL_BUDGET", "Root elapsed-time budget has expired")
        rows = db.execute(
            "SELECT tokens,cost,status FROM reservations WHERE tenant=? AND project=? AND goal_id=?",
            (*scope.keys(), goal_id),
        ).fetchall()
        active = sum(r["status"] == "active" for r in rows)
        if active >= root["max_parallel_works"]:
            raise Hold("PARALLEL_BUDGET", "Root parallel-work limit reached")
        if sum(r["tokens"] for r in rows) + request["max_tokens"] > root["max_tokens"]:
            raise Hold(
                "TOKEN_BUDGET",
                "Actual usage plus outstanding reservations exceed the root token budget",
            )
        cost = request["max_cost_microunits"]
        limit = root["max_cost_microunits"]
        if limit is not None and (
            cost is None
            or any(r["cost"] is None for r in rows)
            or sum(r["cost"] for r in rows) + cost > limit
        ):
            raise Hold(
                "COST_BUDGET", "Known and reserved cost cannot be bounded within the root limit"
            )
        db.execute(
            "INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?,NULL)",
            (
                *scope.keys(),
                goal_id,
                run_id,
                request["max_tokens"],
                cost,
                self.store.clock(),
                request["max_wall_seconds"],
                "active",
            ),
        )

    def settle(self, db, scope: Scope, run_id: str, usage: dict) -> None:
        row = db.execute(
            "SELECT * FROM reservations WHERE tenant=? AND project=? AND run_id=?",
            (*scope.keys(), run_id),
        ).fetchone()
        if not row:
            raise RuntimeFault("RESERVATION_MISSING", "Run has no root budget reservation")
        if not isinstance(usage, dict):
            raise RuntimeFault("USAGE_TYPE", "Usage observation must be an object")
        for k in ("input_tokens", "output_tokens", "cost_microunits"):
            if usage.get(k) is not None and (type(usage[k]) is not int or usage[k] < 0):
                raise RuntimeFault("INVALID_USAGE", "Usage is a nonnegative integer or unknown")
        known = (
            usage.get("status") == "measured"
            and usage.get("input_tokens") is not None
            and usage.get("output_tokens") is not None
        )
        tokens = usage["input_tokens"] + usage["output_tokens"] if known else row["tokens"]
        cost = (
            usage.get("cost_microunits")
            if known and usage.get("cost_microunits") is not None
            else row["cost"]
        )
        if tokens < 0 or (cost is not None and cost < 0):
            raise RuntimeFault("NEGATIVE_USAGE", "Usage cannot be negative")
        status = "settled" if known and usage.get("cost_microunits") is not None else "unknown"
        if tokens > row["tokens"] or (
            row["cost"] is not None and cost is not None and cost > row["cost"]
        ):
            status = "overrun"
        db.execute(
            "UPDATE reservations SET tokens=?,cost=?,status=?,usage=? WHERE tenant=? AND project=? AND run_id=?",
            (tokens, cost, status, json.dumps(usage), *scope.keys(), run_id),
        )
        if status == "overrun":
            self.store.event(
                db, scope, "run", run_id, "budget.overrun", {"tokens": tokens, "cost": cost}
            )

    def totals(self, scope: Scope, goal_id: str) -> dict:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT * FROM reservations WHERE tenant=? AND project=? AND goal_id=?",
                (*scope.keys(), goal_id),
            ).fetchall()
        return {
            "reserved_or_spent_tokens": sum(r["tokens"] for r in rows),
            "reserved_or_spent_cost_microunits": None
            if any(r["cost"] is None for r in rows)
            else sum(r["cost"] for r in rows),
            "unknown_runs": sum(r["status"] == "unknown" for r in rows),
            "overruns": sum(r["status"] == "overrun" for r in rows),
            "active_runs": sum(r["status"] == "active" for r in rows),
        }

    def suspend(self, db, scope: Scope, run_id: str) -> None:
        """Release a compute slot only; unknown token/cost reservation is retained."""
        row = db.execute(
            "SELECT status FROM reservations WHERE tenant=? AND project=? AND run_id=?",
            (*scope.keys(), run_id),
        ).fetchone()
        if not row or row[0] not in {"active", "suspended"}:
            raise Hold("RESERVATION_STATE", "Only active work can suspend")
        db.execute(
            "UPDATE reservations SET status='suspended' WHERE tenant=? AND project=? AND run_id=?",
            (*scope.keys(), run_id),
        )

    def resume(self, db, scope: Scope, run_id: str, root: dict, *, goal_started: float) -> None:
        if self.store.clock() - goal_started >= root["max_wall_seconds"]:
            raise Hold("WALL_BUDGET", "Human wait does not reset the deadline")
        row = db.execute(
            "SELECT * FROM reservations WHERE tenant=? AND project=? AND run_id=?",
            (*scope.keys(), run_id),
        ).fetchone()
        if not row or row["status"] != "suspended":
            raise Hold("RESERVATION_STATE", "An exact suspended reservation is required")
        active = db.execute(
            "SELECT COUNT(*) FROM reservations WHERE tenant=? AND project=? AND goal_id=? AND status='active'",
            (*scope.keys(), row["goal_id"]),
        ).fetchone()[0]
        if active >= root["max_parallel_works"]:
            raise Hold("PARALLEL_BUDGET", "No compute slot is available for resume")
        db.execute(
            "UPDATE reservations SET status='active' WHERE tenant=? AND project=? AND run_id=?",
            (*scope.keys(), run_id),
        )
