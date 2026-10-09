"""A durable proposal-root budget shared by evaluation and canary work.

Reservations precede callbacks. Missing/uncertain usage holds the reservation and
blocks new work until an independent operator reconciles it. This ledger cannot
create execution grants and is never restored by a release-pointer rollback.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.contracts.identity import now
from amplai_foundry.runtime.errors import Conflict, Hold, RuntimeFault
from amplai_foundry.runtime.evidence.cas import ArtifactStore
from amplai_foundry.runtime.storage.store import Scope, Store


class EvolutionBudget:
    def __init__(self, store: Store) -> None:
        self.store = store

    def freeze(
        self,
        db: sqlite3.Connection,
        scope: Scope,
        proposal_id: str,
        experiment_ref: dict[str, Any],
        limits: dict[str, Any],
    ) -> None:
        try:
            head = self.store.head(scope, "meta-budget", proposal_id, db=db)
        except RuntimeFault as exc:
            if exc.code != "NOT_FOUND":
                raise
            head = {
                "row_version": 0,
                "data": {
                    "limits": limits,
                    "experiment_refs": [],
                    "allocations": {},
                    "started_epoch": self.store.clock(),
                    "created_at": now(),
                },
            }
        data = head["data"]
        if data["limits"] != limits:
            raise Hold(
                "META_BUDGET_CHANGE",
                "Changing a proposal root budget requires a separate governed proposal",
            )
        refs = data["experiment_refs"]
        if experiment_ref in refs:
            return
        if len(refs) >= limits["max_attempts"]:
            raise Hold("META_ATTEMPTS", "Preapproved experiment-attempt budget exhausted")
        self.store.cas(
            db,
            scope,
            "meta-budget",
            proposal_id,
            head["row_version"],
            "active",
            {**data, "experiment_refs": [*refs, experiment_ref]},
        )

    def reserve(
        self,
        db: sqlite3.Connection,
        scope: Scope,
        proposal_id: str,
        allocation_id: str,
        *,
        tokens: int,
        cost: int,
        cost_compared: bool = True,
    ) -> None:
        """``cost_compared`` False (D-088) is kept on the allocation, so settlement and a later
        reconciliation both treat a reported cost as ``reported_cost``, never as spending."""
        if any(type(x) is not int or x < 0 for x in (tokens, cost)):
            raise RuntimeFault("META_RESERVATION", "Executor ceilings must be nonnegative integers")
        head = self.store.head(scope, "meta-budget", proposal_id, db=db)
        data, key = head["data"], allocation_id
        a, limits = data["allocations"], data["limits"]
        if key in a:
            raise Conflict(
                "META_ALLOCATION_REPLAY",
                "A dispatched evaluation allocation cannot be silently rerun",
            )
        if any(v["status"] == "unknown" for v in a.values()):
            raise Hold(
                "META_USAGE_UNKNOWN",
                "An uncertain prior allocation must be reconciled before further spending",
            )
        if any(v.get("overrun") for v in a.values()):
            raise Hold("META_PRIOR_OVERRUN", "A prior ceiling violation prevents further admission")
        active = [v for v in a.values() if v["status"] == "reserved"]
        if len(active) >= limits["max_parallel_works"]:
            raise Hold("META_CONCURRENCY", "Root experiment concurrency limit reached")
        if self.store.clock() - data["started_epoch"] >= limits["max_wall_seconds"]:
            raise Hold("META_WALL_BUDGET", "The proposal root wall-time budget is exhausted")
        total_tokens = sum(v["tokens"] for v in a.values())
        total_cost = sum(v["cost"] for v in a.values())
        if total_tokens + tokens > limits["max_tokens"]:
            raise Hold("META_TOKEN_BUDGET", "Reservation exceeds the shared root token budget")
        if (
            limits["max_cost_microunits"] is not None
            and total_cost + cost > limits["max_cost_microunits"]
        ):
            raise Hold("META_COST_BUDGET", "Reservation exceeds the shared root cost budget")
        allocation = {
            "status": "reserved",
            "tokens": tokens,
            "cost": cost,
            "token_ceiling": tokens,
            "cost_ceiling": cost,
            "owner_epoch": self.store.epoch,
            "issued_at": now(),
        }
        if not cost_compared:
            allocation["cost_compared"] = False
        self.store.cas(
            db,
            scope,
            "meta-budget",
            proposal_id,
            head["row_version"],
            "active",
            {**data, "allocations": {**a, key: allocation}},
        )

    def settle(
        self,
        db: sqlite3.Connection,
        scope: Scope,
        proposal_id: str,
        allocation_id: str,
        *,
        tokens: int | None,
        cost: int | None,
        uncertain: bool = False,
        cost_required: bool = True,
    ) -> dict[str, Any]:
        head = self.store.head(scope, "meta-budget", proposal_id, db=db)
        data = head["data"]
        a = data["allocations"]
        if allocation_id not in a or a[allocation_id]["status"] != "reserved":
            raise Conflict("META_SETTLEMENT", "No outstanding reservation for this result")
        old = a[allocation_id]
        if any(v is not None and (type(v) is not int or v < 0) for v in (tokens, cost)):
            raise RuntimeFault(
                "META_USAGE", "Observed usage must be nonnegative integers or unknown"
            )
        # D-088: a plan that does not compare cost keeps the reserved cost in the ledger, so a
        # reported cost (an API-equivalent estimate, D-094) is neither an overrun nor spending
        # against the root cost budget; it is kept as ``reported_cost``. Calibration plans
        # calplan-1bdb0658 (overrun) and calplan-3fd084f0 (META_COST_BUDGET at the next reserve)
        # stopped on Claude trials of 0.2 to 0.4 USD against a cost budget of 0 (2026-10-09).
        counted = cost if cost_required else None
        overrun = (tokens is not None and tokens > old["token_ceiling"]) or (
            counted is not None and counted > old["cost_ceiling"]
        )
        # D-088: when the plan does not compare cost, a missing cost keeps the reserved amount
        # (never 0) and does not make the allocation uncertain; tokens are still required.
        uncertain = uncertain or tokens is None or (cost is None and cost_required)
        kept_cost = old["cost"] if counted is None else counted
        new = {
            **old,
            "status": "unknown" if uncertain else "settled",
            "tokens": max(old["tokens"], tokens or 0) if uncertain else tokens,
            "cost": max(old["cost"], counted or 0) if uncertain else kept_cost,
            "overrun": overrun,
            "settled_at": now(),
        }
        if not cost_required:
            new["cost_compared"] = False  # a later reconcile() applies the same rule
        if counted is None and cost is not None:
            new["reported_cost"] = cost
        self.store.cas(
            db,
            scope,
            "meta-budget",
            proposal_id,
            head["row_version"],
            "active",
            {**data, "allocations": {**a, allocation_id: new}},
        )
        return {"overrun": overrun, "uncertain": uncertain}

    def totals(self, scope: Scope, proposal_id: str) -> dict[str, Any]:
        data = self.store.head(scope, "meta-budget", proposal_id)["data"]
        a = list(data["allocations"].values())
        return {
            "limits": data["limits"],
            "attempts": len(data["experiment_refs"]),
            "reserved_or_spent_tokens": sum(v["tokens"] for v in a),
            "reserved_or_spent_cost_microunits": sum(v["cost"] for v in a),
            "pending": sum(v["status"] == "reserved" for v in a),
            "unknown": sum(v["status"] == "unknown" for v in a),
            "overruns": sum(v.get("overrun", False) for v in a),
        }

    def reconcile(
        self,
        actor: Actor,
        proposal_id: str,
        allocation_id: str,
        *,
        tokens: int,
        cost: int,
        evidence_ref: dict[str, Any],
        artifacts: ArtifactStore,
    ) -> None:
        actor.require("experiment.reconcile")
        if "harness.propose" in actor.permissions:
            raise Hold("SELF_RECONCILE", "A proposer cannot clear uncertain evaluation spending")
        from amplai_foundry.evaluation.receipts import read_receipt

        receipt = read_receipt(artifacts.read(actor.scope, evidence_ref, trusted=True))
        if (
            receipt.get("allocation_id") != allocation_id
            or receipt.get("tokens") != tokens
            or receipt.get("cost_microunits") != cost
            or receipt.get("process_stopped") is not True
            or type(receipt.get("unknown_effects")) is not int
            or receipt["unknown_effects"] != 0
        ):
            raise Hold(
                "RECONCILIATION_EVIDENCE", "Need exact final usage and stopped-process evidence"
            )
        if any(type(x) is not int or x < 0 for x in (tokens, cost)):
            raise RuntimeFault("META_USAGE", "Reconciled counters must be nonnegative integers")
        with self.store.tx() as db:
            head = self.store.head(actor.scope, "meta-budget", proposal_id, db=db)
            a = head["data"]["allocations"]
            old = a.get(allocation_id)
            if not old or old["status"] not in {"reserved", "unknown"}:
                raise Hold("RECONCILIATION_STATE", "No unresolved allocation")
            # D-088 (PR #65 review): an allocation whose plan does not compare cost keeps its
            # reserved cost here too; the receipt's cost is ``reported_cost``, never an overrun
            compared = old.get("cost_compared", True) is not False
            new = {
                **old,
                "status": "settled",
                "tokens": tokens,
                "cost": cost if compared else old["cost"],
                "overrun": tokens > old["token_ceiling"]
                or (compared and cost > old["cost_ceiling"]),
                "reconciliation_ref": evidence_ref,
                "settled_at": now(),
            }
            if not compared:
                new["reported_cost"] = cost
            self.store.cas(
                db,
                actor.scope,
                "meta-budget",
                proposal_id,
                head["row_version"],
                "active",
                {**head["data"], "allocations": {**a, allocation_id: new}},
            )
            self.store.event(
                db,
                actor.scope,
                "release",
                proposal_id,
                "experiment.reconciled",
                {"allocation_id": allocation_id, "evidence_ref": evidence_ref},
            )
