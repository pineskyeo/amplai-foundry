"""Root-goal reservations. Unknown usage is never treated as free usage."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import Any

from ..errors import Hold, RuntimeFault
from ..storage.store import Scope, Store

# D-128: run heads that ended without settling their reservation, and the journal states of a
# driver turn that ended (``agent_drivers/ports.py``)
ENDED_RUN_STATES = frozenset({"failed", "cancelled", "lost"})
ENDED_JOURNAL_STATES = frozenset({"completed", "failed", "cancelled"})


class BudgetService:
    def __init__(self, store: Store) -> None:
        self.store = store

    def reserve(
        self,
        db: sqlite3.Connection,
        scope: Scope,
        goal_id: str,
        run_id: str,
        root: dict[str, Any],
        request: dict[str, Any],
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
            "SELECT tokens,cost,status FROM reservations "
            "WHERE tenant=? AND project=? AND goal_id=?",
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

    def settle(
        self, db: sqlite3.Connection, scope: Scope, run_id: str, usage: dict[str, Any]
    ) -> None:
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
        counted = usage.get("input_tokens") is not None and usage.get("output_tokens") is not None
        known = usage.get("status") == "measured" and counted
        # a driver-estimated cost (e.g. Claude CLI total_cost_usd) comes with provider-reported
        # token counts: those settle and can overrun; the estimate is recorded, never settled,
        # and never blocks work on its own (D-084; design/07:40, design/15:28)
        estimated = usage.get("status") == "estimated" and counted
        tokens = (
            usage["input_tokens"] + usage["output_tokens"] if known or estimated else row["tokens"]
        )
        cost = (
            usage.get("cost_microunits")
            if known and usage.get("cost_microunits") is not None
            else row["cost"]
        )
        if tokens < 0 or (cost is not None and cost < 0):
            raise RuntimeFault("NEGATIVE_USAGE", "Usage cannot be negative")
        status = "settled" if known and usage.get("cost_microunits") is not None else "unknown"
        if estimated:
            status = "estimated"
        if tokens > row["tokens"] or (
            row["cost"] is not None and cost is not None and cost > row["cost"]
        ):
            status = "overrun"
        guess = usage.get("cost_microunits") if estimated else None
        if guess is not None and row["cost"] is not None and guess > row["cost"]:
            self.store.event(
                db, scope, "run", run_id, "budget.estimate_over",
                {"estimated_cost": guess, "reserved_cost": row["cost"]},
            )  # fmt: skip
        db.execute(
            "UPDATE reservations SET tokens=?,cost=?,status=?,usage=? "
            "WHERE tenant=? AND project=? AND run_id=?",
            (tokens, cost, status, json.dumps(usage), *scope.keys(), run_id),
        )
        if status == "overrun":
            self.store.event(
                db, scope, "run", run_id, "budget.overrun", {"tokens": tokens, "cost": cost}
            )

    def settle_ended(
        self, scope: Scope, journal: Callable[[str], dict[str, Any] | None]
    ) -> dict[str, Any]:
        """D-128 (operator decision 2026-10-14): settle the ``active`` reservation of every run
        that ended ``failed``, ``cancelled`` or ``lost`` (``ExecutionService.end_goal`` ends a
        run without settling it) from its driver journal's usage, the record the worker settles
        a finished run from. ``journal(dispatch_id)`` reads one journal (None: none).

        Settled only when every check holds, else listed under ``skipped`` with its reason and
        left ``active``: the run has exactly one worker dispatch; its journal exists, names that
        dispatch, ended (``completed``, ``failed`` or ``cancelled``) with ``process_stopped``
        true, has no follow-up or candidate journal (``<id>-f1``, ``<id>-c1``: their usage would
        be left out), and reports input and output token counts. ``settle`` then applies its
        usual rules (an estimated usage settles ``estimated``; above the reservation is an
        ``overrun``). A run still running is never touched."""
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT r.run_id,r.goal_id,h.state FROM reservations r JOIN heads h "
                "ON h.tenant=r.tenant AND h.project=r.project AND h.kind='run' AND h.id=r.run_id "
                "WHERE r.tenant=? AND r.project=? AND r.status='active' ORDER BY r.run_id",
                scope.keys(),
            ).fetchall()
            ended = [dict(r) for r in rows if r["state"] in ENDED_RUN_STATES]
            dispatches = {
                row["run_id"]: [
                    d["dispatch_id"]
                    for d in self.store.conn.execute(
                        "SELECT dispatch_id FROM worker_dispatch "
                        "WHERE tenant=? AND project=? AND run_id=?",
                        (*scope.keys(), row["run_id"]),
                    ).fetchall()
                ]
                for row in ended
            }
        settled: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for row in ended:
            run_id, ids = row["run_id"], dispatches[row["run_id"]]

            def skip(reason: str, run_id: str = run_id) -> None:
                skipped.append({"run_id": run_id, "reason": reason})

            if len(ids) != 1:
                skip(f"dispatches:{len(ids)}")
                continue
            dispatch_id = ids[0]
            record = journal(dispatch_id)
            usage = (record or {}).get("usage")
            if record is None:
                skip("no_journal")
            elif record.get("dispatch_id") != dispatch_id:
                skip("journal_mismatch")
            elif (
                record.get("state") not in ENDED_JOURNAL_STATES
                or record.get("process_stopped") is not True
            ):
                skip("process_not_stopped")
            elif (
                journal(dispatch_id + "-f1") is not None or journal(dispatch_id + "-c1") is not None
            ):
                skip("multi_turn")
            elif not isinstance(usage, dict) or any(
                type(usage.get(k)) is not int for k in ("input_tokens", "output_tokens")
            ):
                skip("no_usage")
            else:
                with self.store.tx() as db:
                    current = db.execute(
                        "SELECT status FROM reservations WHERE tenant=? AND project=? AND run_id=?",
                        (*scope.keys(), run_id),
                    ).fetchone()
                    if current is None or current["status"] != "active":
                        skip("not_active")
                        continue
                    self.settle(db, scope, run_id, dict(usage))
                    status = db.execute(
                        "SELECT tokens,status FROM reservations "
                        "WHERE tenant=? AND project=? AND run_id=?",
                        (*scope.keys(), run_id),
                    ).fetchone()
                    self.store.event(
                        db, scope, "run", run_id, "budget.settled_from_journal",
                        {"dispatch_id": dispatch_id, "tokens": status["tokens"],
                         "status": status["status"]},
                    )  # fmt: skip
                settled.append(
                    {"run_id": run_id, "dispatch_id": dispatch_id, "tokens": status["tokens"],
                     "status": status["status"]}
                )  # fmt: skip
        return {"settled": settled, "skipped": skipped}

    def totals(self, scope: Scope, goal_id: str) -> dict[str, Any]:
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
            "estimated_runs": sum(r["status"] == "estimated" for r in rows),
            "overruns": sum(r["status"] == "overrun" for r in rows),
            "active_runs": sum(r["status"] == "active" for r in rows),
        }

    def suspend(self, db: sqlite3.Connection, scope: Scope, run_id: str) -> None:
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

    def resume(
        self,
        db: sqlite3.Connection,
        scope: Scope,
        run_id: str,
        root: dict[str, Any],
        *,
        goal_started: float,
    ) -> None:
        if self.store.clock() - goal_started >= root["max_wall_seconds"]:
            raise Hold("WALL_BUDGET", "Human wait does not reset the deadline")
        row = db.execute(
            "SELECT * FROM reservations WHERE tenant=? AND project=? AND run_id=?",
            (*scope.keys(), run_id),
        ).fetchone()
        if not row or row["status"] != "suspended":
            raise Hold("RESERVATION_STATE", "An exact suspended reservation is required")
        active = db.execute(
            "SELECT COUNT(*) FROM reservations "
            "WHERE tenant=? AND project=? AND goal_id=? AND status='active'",
            (*scope.keys(), row["goal_id"]),
        ).fetchone()[0]
        if active >= root["max_parallel_works"]:
            raise Hold("PARALLEL_BUDGET", "No compute slot is available for resume")
        db.execute(
            "UPDATE reservations SET status='active' WHERE tenant=? AND project=? AND run_id=?",
            (*scope.keys(), run_id),
        )
