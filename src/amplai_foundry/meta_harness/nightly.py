"""The nightly runner: L10 experiment operations (Work 033 S12).

interfaces.md §2.14, §3.13, §8.6-§8.10, IC-10, IC-13, IC-17, IC-18; ``plan.md`` §9, §10.5.

IC-17 and IC-18 are **provisional**: nothing runs a night until the human operator issues a
standing approval (``amplai meta nightly approve``, action ``nightly.explore``, permission
``nightly.approve``, ``LocalMetaApprovals.issue_nightly``). Without a current one ``run`` holds
``NIGHT_STOPPED`` before it writes anything.

- **Identity.** The runner acts as the service identity ``amplai-meta-nightly``
  (``meta_local.nightly_actor``). Every exact approval it uses comes from
  ``LocalMetaApprovals.issue_standing`` (``NightlyRunner.derive``), which admits only exploratory
  development experiments, calibrations and regression-set drift checks within the standing
  policy's cells and ``max_budget``. It never obtains a confirmatory, holdout, canary or promotion
  approval; confirmation runs only experiments the operator approved (IC-10).
- **Deployment.** It runs on a separate meta deployment (its own ``local.json`` with
  ``meta.nightly`` and its own store; the store has one owner, ``Store`` ``ACTIVE_OWNER``), never on
  the running server's (§8.8).
- **Night length**: a night ends at ``min(stop_at, start + max_hours)`` (``max_hours`` default 8;
  clarification after S12).
- **Phases** (in order): preflight (kill switch, cells and evaluator qualified, the IC-29
  executor qualification record of each cell, stop time) → drift (each cell's champion: snapshot
  change, then ``drift_tasks``: drift iff the pass count k of n lies outside the two-sided 99 %
  binomial prediction range at the ends of the calibrated 95 % Wilson interval, ``drift_range``) →
  screening design (PB12, or its fold-over mirror on the next screening night, when the operator set
  ``shares.screening_design`` > 0) → search (proposer drafts, surrogate ranking, successive halving)
  → confirmation (only operator-approved experiments; otherwise its share returns to search) →
  dreaming → dashboard refresh (``meta_harness.dashboard``; ``skipped`` when the backend
  builds no pages).
- **Stops.** Budget (a normal end), ``stop_at``, a quota signal, a revoked or expired standing
  approval (checked by the runner's guard before every trial unit and by
  ``LocalMetaApprovals.check`` at every trial guard of the evaluation services), the kill switch,
  a failed preflight, drift (search, screening and confirmation stop; dreaming and the dashboard
  still run; re-calibration and a removal sweep are scheduled; the band is the operator's
  calibration, never a night's own drift run, and later nights stay stopped with
  ``recalibration_pending`` until an operator calibration newer than the drifted night exists),
  and the first unknown effect
  (IC-18: the root is listed in ``reconcile_pending``). Every stop but the budget ends ``run`` with
  ``Hold NIGHT_STOPPED`` (details: ``reason``, ``run_ref``) after the records are written.
- **Records.** ``nightly-plan`` (``nightplan-<date>``), the ``nightly-run`` head (``night-<date>``)
  and, at the end, a ``nightly-run`` record of the same id (the ref ``run`` returns), and
  ``quota-observation`` (``QuotaObserver.record``). A dry run writes ``nightplan-<date>-dry`` and
  ``night-<date>-dry`` instead, dispatches no trial, derives no approval, runs no model turn and
  writes no quota observation.

The phase actions are a ``NightlyBackend``: ``LocalNightlyBackend`` on the meta deployment, or a
fake in tests. The runner owns ordering, budget shares, stop conditions, the drift rule, the
surrogate ranking and successive halving, and the records.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from statistics import NormalDist
from typing import TYPE_CHECKING, Any, Protocol

from ..evaluation.sequential import wilson
from ..runtime.contracts.identity import digest, now
from ..runtime.contracts.semantics import resolve_ref
from ..runtime.errors import Hold, RuntimeFault
from ..runtime.execution.meta_local import (
    APPROVAL_KIND,
    NIGHTLY_ACTION,
    STANDING_ALLOWED,
    epoch_of,
    nightly_actor,
    regression_corpus_refs,
    validate_nightly_policy,
)
from .quota import QuotaObserver
from .surrogate import AdditiveSurrogate, fold_over, plackett_burman_12, successive_halving

if TYPE_CHECKING:
    from ..runtime.local_deployment import LocalProductDeployment

PLAN_KIND = "nightly-plan"
RUN_KIND = "nightly-run"
PLAN_SCHEMA = "amplai.nightly-plan.v1"
RUN_SCHEMA = "amplai.nightly-run.v1"
PHASES = (
    "preflight", "drift", "screening_design", "search", "confirmation", "dreaming", "dashboard",
)  # fmt: skip
SHARE_KEYS = ("drift", "screening_design", "search", "confirmation")
# stop reasons that end ``run`` with Hold NIGHT_STOPPED; "budget" is a normal end
HOLD_REASONS = frozenset(
    {"no_standing_approval", "standing_approval", "stop_at", "quota_signal", "kill_switch",
     "preflight", "drift", "unknown_effect", "error"}
)  # fmt: skip
# after these the night runs no further model turn (dreaming is skipped)
NO_DREAM = frozenset(
    {"standing_approval", "stop_at", "quota_signal", "kill_switch", "preflight", "unknown_effect",
     "error"}
)  # fmt: skip
APPROVAL_CODES = frozenset({"STANDING_APPROVAL", "META_APPROVAL"})
SNAPSHOT_FIELDS = ("provider_model_id", "driver_version", "image")
ARMS = 2  # a search trial unit pairs the candidate with the champion
Z95 = NormalDist().inv_cdf(0.975)
PB12_FACTORS = 11
DEFAULT_MAX_HOURS = 8
# clarification after S12: drift is a pass count outside the two-sided 99 % binomial prediction
# range at the ends of the calibrated 95 % Wilson interval
DRIFT_LOW_Q, DRIFT_HIGH_Q = 0.005, 0.995

Guard = Callable[[int], None]


# -- config ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class NightlyConfig:  # keys = local.json meta.nightly (§12.2), shape §2.14
    budget_trials: int
    shares: dict[str, float]
    cells: tuple[str, ...]
    drift_tasks: tuple[str, ...]
    pilot: bool
    pilot_nights: int
    keep_operator_share: float
    max_parallel: int
    stop_at: str  # "HH:MM" local time
    # clarification after S12: a night ends at min(stop_at, start + max_hours) (default 8)
    max_hours: float = DEFAULT_MAX_HOURS

    def __post_init__(self) -> None:
        bad = []
        if type(self.budget_trials) is not int or self.budget_trials < 0:
            bad.append("budget_trials")
        s = self.shares
        if (
            not isinstance(s, dict)
            or set(s) != set(SHARE_KEYS)
            or not all(type(v) in (int, float) and 0 <= v <= 1 for v in s.values())
            or abs(s["drift"] + s["search"] + s["confirmation"] - 1) > 1e-9
            or s["screening_design"] > s["search"]
        ):
            bad.append("shares")
        if not all(isinstance(c, str) and c for c in self.cells) or len(set(self.cells)) != len(
            self.cells
        ):
            bad.append("cells")
        if not all(isinstance(t, str) and t for t in self.drift_tasks):
            bad.append("drift_tasks")
        if type(self.pilot_nights) is not int or self.pilot_nights < 0:
            bad.append("pilot_nights")
        if type(self.keep_operator_share) not in (int, float) or not (
            0 <= self.keep_operator_share <= 1
        ):
            bad.append("keep_operator_share")
        if type(self.max_parallel) is not int or not 1 <= self.max_parallel <= 4:
            bad.append("max_parallel")
        if _stop_minutes(self.stop_at) is None:
            bad.append("stop_at")
        if type(self.max_hours) not in (int, float) or not 0 < self.max_hours <= 24:
            bad.append("max_hours")
        if bad:
            raise RuntimeFault("NIGHTLY_CONFIG", "Invalid nightly config (§2.14, §12.2)",
                               details=bad)  # fmt: skip

    @classmethod
    def from_entry(cls, entry: Any) -> NightlyConfig:
        """From ``local.json`` ``meta.nightly`` (``local_deployment.NightlyEntry``) or a dict."""
        get: Callable[[str], Any] = (
            entry.get if isinstance(entry, dict) else lambda k: getattr(entry, k)
        )
        return cls(
            budget_trials=get("budget_trials"),
            shares=dict(get("shares")),
            cells=tuple(get("cells")),
            drift_tasks=tuple(get("drift_tasks")),
            pilot=bool(get("pilot")),
            pilot_nights=get("pilot_nights"),
            keep_operator_share=get("keep_operator_share"),
            max_parallel=get("max_parallel"),
            stop_at=get("stop_at"),
            max_hours=_max_hours(entry),
        )


def _max_hours(entry: Any) -> Any:
    """``meta.nightly.max_hours`` when the entry carries it (``local_deployment.NightlyEntry``,
    0 < h <= 24), else the default 8."""
    value = entry.get("max_hours") if isinstance(entry, dict) else getattr(entry, "max_hours", None)
    return DEFAULT_MAX_HOURS if value is None else value


def _stop_minutes(value: Any) -> int | None:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return None
    hh, mm = value[:2], value[3:]
    if not (hh.isdigit() and mm.isdigit()) or int(hh) > 23 or int(mm) > 59:
        return None
    return int(hh) * 60 + int(mm)


def night_end(start: float, stop_at: str, max_hours: float = DEFAULT_MAX_HOURS) -> float:
    """When a night started at ``start`` ends: min(the next ``stop_at``, start + ``max_hours``)
    (clarification after S12, "Night length")."""
    return min(stop_epoch(start, stop_at), start + float(max_hours) * 3600)


def binomial_quantile(n: int, p: float, q: float) -> int:
    """Bin⁻¹(n, p)(q): the smallest k with P(X <= k) >= q for X ~ Binomial(n, p)."""
    if type(n) is not int or n < 0 or not 0 <= p <= 1 or not 0 < q < 1:
        raise RuntimeFault("NIGHT_DRIFT", "binomial_quantile needs n >= 0, 0 <= p <= 1, 0 < q < 1")
    total = 0.0
    for k in range(n + 1):
        total += math.comb(n, k) * p**k * (1 - p) ** (n - k)
        if total >= q - 1e-12:
            return k
    return n


def drift_range(n: int, wilson_95: tuple[float, float] | list[float]) -> tuple[int, int]:
    """The pass counts of ``n`` drift trials inside the drift rule: [Bin⁻¹(n, p_lo)(0.005),
    Bin⁻¹(n, p_hi)(0.995)] at the ends (p_lo, p_hi) of the calibrated 95 % Wilson interval; a
    count below the first or above the second is drift."""
    low, high = (min(1.0, max(0.0, float(x))) for x in wilson_95)
    return binomial_quantile(n, low, DRIFT_LOW_Q), binomial_quantile(n, high, DRIFT_HIGH_Q)


def stop_epoch(start: float, stop_at: str) -> float:
    """The first local ``stop_at`` after ``start`` (epoch seconds)."""
    minutes = _stop_minutes(stop_at)
    if minutes is None:
        raise RuntimeFault("NIGHTLY_CONFIG", "stop_at is HH:MM")
    begin = datetime.fromtimestamp(start).astimezone()
    at = begin.replace(hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0)
    if at.timestamp() <= start:
        at += timedelta(days=1)
    return at.timestamp()


def nightly_policy(
    config: NightlyConfig,
    *,
    budget_trials: int,
    valid_from: str,
    valid_until: str,
    max_budget: dict[str, Any],
) -> dict[str, Any]:
    """The nightly policy a standing approval binds (§8.8); validated (NIGHTLY_POLICY)."""
    policy = {
        "cells": list(config.cells),
        "budget_trials": budget_trials,
        "shares": dict(config.shares),
        "allowed": [dict(a) for a in STANDING_ALLOWED],
        "max_budget": dict(max_budget),
        "valid_from": valid_from,
        "valid_until": valid_until,
    }
    validate_nightly_policy(policy)
    return policy


def phase_budget(budget: int, shares: dict[str, float], *, screening: bool) -> dict[str, int]:
    """Trials per phase: drift = floor(B x drift), screening design = floor(B x screening_design)
    taken out of the search share (only when ``screening``), search = floor(B x search) minus
    it, confirmation = the rest (so the four add up to B)."""
    drift = math.floor(budget * shares["drift"])
    search_all = math.floor(budget * shares["search"])
    design = min(search_all, math.floor(budget * shares["screening_design"])) if screening else 0
    return {
        "drift": drift,
        "screening_design": design,
        "search": search_all - design,
        "confirmation": budget - drift - search_all,
    }


# -- the backend ----------------------------------------------------------------------------------
class NightlyBackend(Protocol):
    """The phase actions of one night. A *batch* is ``{"trials": int, "passes": int, "runs": int,
    "unknown_effects": int, "stopped": str|None, "root": str|None, ...}``; ``guard(n)`` is called
    before dispatching ``n`` trials and raises ``Hold NIGHT_STOPPED``; ``limit`` is the most trials
    the call may dispatch."""

    def kill_switch(self) -> bool: ...
    def quota_signal(self, since: float) -> bool: ...
    def preflight(self, cells: tuple[str, ...]) -> list[dict[str, Any]]: ...
    # {"current", "calibrated", "band", "calibrated_at"}: the operator's baseline calibration only
    def snapshot(self, cell_id: str, drift_tasks: tuple[str, ...]) -> dict[str, Any]: ...
    def drift_run(self, cell_id: str, tasks: list[str], guard: Guard, limit: int
                  ) -> dict[str, Any]: ...  # fmt: skip
    def schedule_recalibration(self, cell_id: str, reason: str) -> dict[str, Any]: ...
    def removal_sweep(self, cell_id: str, reason: str) -> list[str]: ...
    def screening_factors(self, cell_id: str) -> list[str]: ...
    def screening_run(self, cell_id: str, configs: list[dict[str, str]], guard: Guard,
                      limit: int) -> dict[str, Any]: ...  # fmt: skip
    def propose(self, cell_id: str) -> list[str]: ...
    def search_candidates(self, cell_id: str) -> list[dict[str, Any]]: ...
    def search_rows(self, cell_id: str) -> list[dict[str, Any]]: ...
    def development_tasks(self, cell_id: str) -> list[str]: ...
    def search_run(self, cell_id: str, candidate_id: str, tasks: int, guard: Guard,
                   limit: int) -> dict[str, Any]: ...  # fmt: skip
    # the frozen confirmatory experiment's ref (§2.14), never approved by the night
    def queue_confirmation(self, cell_id: str, candidate_id: str) -> dict[str, Any] | None: ...
    def confirmations(self) -> list[dict[str, Any]]: ...
    def confirmation_run(self, item: dict[str, Any], guard: Guard, limit: int
                         ) -> dict[str, Any]: ...  # fmt: skip
    def dream(self, cell_id: str, night: str) -> dict[str, Any] | None: ...
    def dashboard(self) -> list[str] | None: ...


class _Stop(Exception):
    def __init__(self, reason: str, details: object = None) -> None:
        super().__init__(reason)
        self.reason, self.details = reason, details


@dataclass
class _Night:
    date: str
    dry_run: bool
    head_id: str
    standing_ref: dict[str, Any]
    plan_ref: dict[str, Any]
    plan: dict[str, Any]
    started: float
    stop_at: float
    caps: dict[str, int]
    data: dict[str, Any]
    version: int = 0
    phase_used: dict[str, int] = field(default_factory=dict)
    search_blocked: bool = False


def _batch(value: Any) -> dict[str, Any]:
    """A backend batch with its counts checked (RuntimeFault NIGHT_BATCH)."""
    if not isinstance(value, dict):
        raise RuntimeFault("NIGHT_BATCH", "A backend batch is an object")
    out = dict(value)
    for key in ("trials", "passes", "runs", "unknown_effects"):
        out.setdefault(key, 0)
        if type(out[key]) is not int or out[key] < 0:
            raise RuntimeFault("NIGHT_BATCH", f"batch.{key} is a nonnegative integer")
    out.setdefault("stopped", None)
    out.setdefault("root", None)
    return out


class NightlyRunner:
    def __init__(
        self,
        dep: LocalProductDeployment,
        config: NightlyConfig,
        *,
        clock: Callable[[], float] = time.time,
        backend: NightlyBackend | None = None,
    ) -> None:
        """``backend`` defaults to ``LocalNightlyBackend.open(dep, self)`` on first use."""
        if not isinstance(config, NightlyConfig):
            raise RuntimeFault("NIGHTLY_CONFIG", "config is a NightlyConfig")
        self.dep, self.config, self.clock = dep, config, clock
        self.store, self.scope = dep.store, dep.scope
        self.approvals = dep.meta_local.approvals
        self.identity = nightly_actor(self.scope)
        self._backend = backend
        self._standing_ref: dict[str, Any] | None = None
        self.quota = QuotaObserver(self.store, self.scope)

    @property
    def backend(self) -> NightlyBackend:
        if self._backend is None:
            self._backend = LocalNightlyBackend.open(self.dep, self)
        return self._backend

    # -- authority ------------------------------------------------------------------------------
    def standing(self) -> dict[str, Any]:
        """The current standing approval ref (Hold NIGHT_STOPPED ``no_standing_approval``)."""
        ref = self.approvals.current_standing()
        if ref is None:
            raise Hold(
                "NIGHT_STOPPED",
                "No current standing approval: amplai meta nightly approve (IC-17, provisional)",
                details={"reason": "no_standing_approval"},
            )
        return dict(ref)

    def derive(self, plan: dict[str, Any]) -> dict[str, Any]:
        """The exact approval of ``plan`` from the standing approval (``issue_standing``)."""
        if self._standing_ref is None:
            raise Hold("STANDING_APPROVAL", "No night is running under a standing approval")
        return dict(
            self.approvals.issue_standing(
                self.identity, self._standing_ref, "experiment.execute", plan
            )
        )

    # -- the plan ---------------------------------------------------------------------------------
    def _earlier_nights(self, date: str) -> list[dict[str, Any]]:
        out = []
        for ref, value in self.store.list_objects(self.scope, RUN_KIND):
            night = value.get("date")
            if not value.get("dry_run") and isinstance(night, str) and night < date:
                out.append({"ref": ref, **value})
        return sorted(out, key=lambda v: (str(v.get("date")), v["ref"]["revision"]))

    def plan_night(self, date: str, *, dry_run: bool = False) -> dict[str, Any]:
        """Write the ``nightly-plan`` of ``date`` under the current standing approval: Hold
        STANDING_APPROVAL when the config asks more than the policy grants (budget, cells,
        shares)."""
        _check_date(date)
        standing_ref = self.standing()
        policy = self.approvals.standing(standing_ref)["policy"]
        cfg = self.config
        problems = {}
        if cfg.budget_trials > policy["budget_trials"]:
            problems["budget_trials"] = [cfg.budget_trials, policy["budget_trials"]]
        if set(cfg.cells) - set(policy["cells"]) or not cfg.cells:
            problems["cells"] = sorted(set(cfg.cells) - set(policy["cells"]))
        if dict(cfg.shares) != dict(policy["shares"]):
            problems["shares"] = [dict(cfg.shares), dict(policy["shares"])]
        if problems:
            raise Hold("STANDING_APPROVAL", "The night asks more than the standing policy grants",
                       details=problems)  # fmt: skip
        earlier = [v for v in self._earlier_nights(date) if v.get("finished_at")]
        quota_ref = self.quota.latest()
        plan = {
            "schema": PLAN_SCHEMA,
            "scope": self.scope.wire(),
            "date": date,
            "budget_trials": cfg.budget_trials,
            "shares": dict(cfg.shares),
            "cells": list(cfg.cells),
            "quota_ref": quota_ref,
            "pilot": bool(cfg.pilot and len(earlier) < cfg.pilot_nights),
            "standing_ref": {k: standing_ref[k] for k in ("id", "revision", "digest")},
            "dry_run": dry_run,
            "created_at": now(),
        }
        plan_id = f"nightplan-{date}" + ("-dry" if dry_run else "")
        revision = 1 + max(
            (r["revision"] for r, _v in self.store.list_objects(self.scope, PLAN_KIND)
             if r["id"] == plan_id),
            default=0,
        )  # fmt: skip
        if revision > 1 and not dry_run:
            raise Hold("NIGHT_STATE", "This night is already planned", details={"date": date})
        with self.store.tx() as db:
            ref: dict[str, Any] = self.store.put(db, self.scope, PLAN_KIND, plan_id, revision, plan)
        return ref

    # -- drift ------------------------------------------------------------------------------------
    def drift_check(self, cell_id: str) -> dict[str, Any]:
        """The champion's snapshot now vs at calibration, and the calibrated band of the drift
        tasks (no trial runs here). ``status``: ``changed`` | ``ok`` | ``uncalibrated``."""
        seen = self.backend.snapshot(cell_id, self.config.drift_tasks)
        current = dict(seen.get("current") or {})
        calibrated = seen.get("calibrated")
        changed = (
            [k for k in SNAPSHOT_FIELDS if current.get(k) != calibrated.get(k)]
            if isinstance(calibrated, dict) else []
        )  # fmt: skip
        band = seen.get("band")
        interval = None
        if isinstance(band, dict) and type(band.get("runs")) is int and band["runs"] > 0:
            interval = list(wilson(int(band["passes"]), int(band["runs"]), Z95))
        status = "changed" if changed else ("ok" if interval is not None else "uncalibrated")
        return {
            "cell_id": cell_id,
            "snapshot": current,
            "calibrated_snapshot": calibrated if isinstance(calibrated, dict) else None,
            "changed": changed,
            "band": {**dict(band), "wilson_95": interval}
            if interval is not None and isinstance(band, dict)
            else None,
            "status": status,
            "calibrated_at": seen.get("calibrated_at"),
        }

    def recalibration_pending(self, cell_id: str, date: str, calibrated_at: Any) -> str | None:
        """The date of the first earlier night that scheduled a re-calibration of ``cell_id``
        which no later operator calibration answers, or None. The baseline calibration
        (``calibrated_at``, the operator's newest; ``LocalNightlyBackend._calibration``) answers
        a night only when it was summarized after that night started; without a known baseline
        time the re-calibration stays pending (§8.8: drift stops search until re-calibration)."""
        base = epoch_of(calibrated_at)
        for value in self._earlier_nights(date):
            entries = [
                e for e in value.get("recalibration") or []
                if isinstance(e, dict) and e.get("cell_id") == cell_id and not e.get("dry_run")
            ]  # fmt: skip
            if not entries:
                continue
            started = epoch_of(value.get("started_at"))
            if base is None or started is None or base <= started:
                return str(value.get("date"))
        return None

    # -- the night --------------------------------------------------------------------------------
    def run(self, date: str, *, dry_run: bool = False) -> dict[str, Any]:
        """Run (or dry-run) the night of ``date``; returns the ``nightly-run`` record ref. Acts as
        ``amplai-meta-nightly`` (IC-17). See the module docstring for phases and stops."""
        _check_date(date)
        head_id = f"night-{date}" + ("-dry" if dry_run else "")
        existing = self._head(head_id)
        if existing is not None and not dry_run:
            raise Hold("NIGHT_STATE", "This night already ran (an interrupted night is not "
                       "resumed; reconcile its roots)",
                       details={"state": existing["state"]})  # fmt: skip
        standing_ref = self.standing()
        self._standing_ref = standing_ref
        try:
            return self._run(date, dry_run, head_id, standing_ref, existing)
        finally:
            self._standing_ref = None

    def _run(
        self,
        date: str,
        dry_run: bool,
        head_id: str,
        standing_ref: dict[str, Any],
        existing: dict[str, Any] | None,
    ) -> dict[str, Any]:
        plan_ref = self.plan_night(date, dry_run=dry_run)
        plan = self.store.get(self.scope, PLAN_KIND, plan_ref)
        started = float(self.clock())
        screening = plan["shares"]["screening_design"] > 0
        night = _Night(
            date=date, dry_run=dry_run, head_id=head_id, standing_ref=standing_ref,
            plan_ref=plan_ref, plan=plan, started=started,
            stop_at=night_end(started, self.config.stop_at, self.config.max_hours),
            caps=phase_budget(plan["budget_trials"], plan["shares"], screening=screening),
            data={
                "schema": RUN_SCHEMA, "scope": self.scope.wire(), "date": date,
                "plan_ref": plan_ref, "phase": "preflight", "trials": 0, "stopped": None,
                "drift": {}, "queued_confirmations": [], "proposals": [], "dreaming_ref": None,
                "finished_at": None, "dry_run": dry_run, "started_at": now(), "phases": [],
                "reconcile_pending": [], "recalibration": [], "findings": [],
            },
            version=existing["row_version"] if existing is not None else 0,
        )  # fmt: skip
        self._save(night, "running")
        error: BaseException | None = None
        try:
            self._phases(night)
        except _Stop as stop:
            # the phase the stop interrupted is recorded too (a phase that ends on its own
            # records itself, e.g. drift)
            current = night.data["phase"]
            last = night.data["phases"][-1]["phase"] if night.data["phases"] else None
            if current != last:
                night.data["phases"].append(
                    {"phase": current, "state": "stopped", "at": now(), "reason": stop.reason}
                )
            self._stopped(night, stop.reason, stop.details)
        except Exception as exc:  # recorded, then re-raised after the night is closed
            error = exc
            self._stopped(night, "error", {"code": getattr(exc, "code", type(exc).__name__)})
        self._tail(night)
        ref = self._finish(night)
        if error is not None:
            raise error
        reason = night.data["stopped"]
        if reason in HOLD_REASONS:
            raise Hold(
                "NIGHT_STOPPED", f"The night stopped: {reason}",
                details={"reason": reason, "run_ref": ref,
                         "reconcile_pending": list(night.data["reconcile_pending"])},
            )  # fmt: skip
        return ref

    def _phases(self, night: _Night) -> None:
        night.data["phase"] = "preflight"
        self._preflight(night)
        night.data["phase"] = "drift"
        self._drift(night)
        night.data["phase"] = "screening_design"
        self._screening(night)
        confirmations = self._confirmations_due(night)
        night.data["phase"] = "search"
        self._search(night, extra=0 if confirmations else night.caps["confirmation"])
        night.data["phase"] = "confirmation"
        self._confirm(night, confirmations)

    def _tail(self, night: _Night) -> None:
        """Dreaming and the dashboard refresh run after any stop (dreaming not after one that
        forbids further model turns); a failure there is a finding."""
        for name, action in (("dreaming", self._dream), ("dashboard", self._dashboard)):
            try:
                action(night)
            except Exception as exc:  # recorded; the night's records still close
                self._phase(night, name, "failed", code=getattr(exc, "code", type(exc).__name__))

    # -- records ----------------------------------------------------------------------------------
    def _head(self, head_id: str) -> dict[str, Any] | None:
        try:
            return dict(self.store.head(self.scope, RUN_KIND, head_id))
        except RuntimeFault as exc:
            if exc.code != "NOT_FOUND":
                raise
            return None

    def _save(self, night: _Night, state: str) -> None:
        with self.store.tx() as db:
            night.version = self.store.cas(
                db, self.scope, RUN_KIND, night.head_id, night.version, state, night.data
            )

    def _phase(self, night: _Night, name: str, state: str, **details: Any) -> None:
        night.data["phase"] = name
        night.data["phases"].append({"phase": name, "state": state, "at": now(), **details})
        self._save(night, "running")

    def _stopped(self, night: _Night, reason: str, details: object = None) -> None:
        if night.data["stopped"] is None:
            night.data["stopped"] = reason
            night.data["stop_details"] = details
        self._save(night, "running")

    def _finish(self, night: _Night) -> dict[str, Any]:
        if not night.dry_run:
            try:
                night.data["quota_refs"] = self.quota.record(night.date)
            except Exception as exc:  # the observation is evidence, never a reason to fail
                night.data["findings"].append(
                    "QUOTA_RECORD: " + str(getattr(exc, "code", type(exc).__name__))
                )
        night.data["finished_at"] = now()
        reason = night.data["stopped"]
        state = "stopped" if reason in HOLD_REASONS else "finished"
        self._save(night, state)
        revision = 1 + max(
            (r["revision"] for r, _v in self.store.list_objects(self.scope, RUN_KIND)
             if r["id"] == night.head_id),
            default=0,
        )  # fmt: skip
        with self.store.tx() as db:
            ref: dict[str, Any] = self.store.put(
                db, self.scope, RUN_KIND, night.head_id, revision, {**night.data, "state": state}
            )
        return ref

    # -- guards -----------------------------------------------------------------------------------
    def _guard(self, night: _Night, phase: str) -> Guard:
        def guard(trials: int) -> None:
            if type(trials) is not int or trials < 0:
                raise RuntimeFault("NIGHT_GUARD", "guard takes a trial count")
            reason = self._night_reason(night)
            if reason is None and night.data["trials"] + trials > night.plan["budget_trials"]:
                reason = "budget"
            if reason is None and night.phase_used.get(phase, 0) + trials > self._cap(night, phase):
                reason = "phase_share"
            if reason is not None:
                raise Hold("NIGHT_STOPPED", f"Night guard: {reason}", details={"reason": reason})

        return guard

    def _night_reason(self, night: _Night) -> str | None:
        try:
            self.approvals.standing(night.standing_ref)
        except Hold:
            return "standing_approval"
        if float(self.clock()) >= night.stop_at:
            return "stop_at"
        if self.backend.kill_switch():
            return "kill_switch"
        if self.backend.quota_signal(night.started):
            return "quota_signal"
        return None

    def _cap(self, night: _Night, phase: str) -> int:
        return int(night.caps.get(phase, 0))

    def _remaining(self, night: _Night, phase: str) -> int:
        return max(
            0,
            min(
                self._cap(night, phase) - night.phase_used.get(phase, 0),
                int(night.plan["budget_trials"]) - int(night.data["trials"]),
            ),
        )

    def _check(self, night: _Night) -> None:
        """Between units: a night-ending reason stops the night; an exhausted budget too."""
        reason = self._night_reason(night)
        if reason is not None:
            raise _Stop(reason)
        if night.data["trials"] >= night.plan["budget_trials"] and night.plan["budget_trials"]:
            raise _Stop("budget")

    def _unit(
        self,
        night: _Night,
        phase: str,
        call: Callable[[Guard, int], Any],
        label: str,
        *,
        cap: int | None = None,
    ) -> dict[str, Any] | None:
        """Run one backend unit under the night guard and account its batch. A guard stop of
        the night propagates (``_Stop``); a phase-share or budget guard ends the unit (None); a
        unit refused by another Hold is a finding (None). A Hold that carries the batch of what
        the unit ran before it (``details.partial``) has that batch accounted first, and the unit
        returns it instead of None."""
        self._check(night)
        limit = self._remaining(night, phase)
        if cap is not None:
            limit = min(limit, cap)
        try:
            batch = _batch(call(self._guard(night, phase), limit))
        except Hold as exc:
            details = exc.details if isinstance(exc.details, dict) else {}
            reason = details.get("reason")
            done = self._partial(night, phase, exc.code, details.get("partial"))
            if exc.code == "NIGHT_STOPPED" and reason == "phase_share":
                return done  # this phase's share is spent; the night goes on
            if exc.code == "NIGHT_STOPPED" and isinstance(reason, str):
                raise _Stop(reason) from exc
            if exc.code in APPROVAL_CODES and self._night_reason(night) == "standing_approval":
                raise _Stop("standing_approval") from exc
            night.data["findings"].append(f"{label}: {exc.code}")
            self._save(night, "running")
            return done
        self._account(night, phase, batch)
        return batch

    def _partial(self, night: _Night, phase: str, code: str, partial: Any) -> dict[str, Any] | None:
        """Account the trials a unit ran before a Hold ended it (budget, phase share, IC-18):
        an unknown effect among them, a revoked standing approval or the kill switch (the Hold's
        code when the batch names no stop) stops the night here."""
        if partial is None:
            return None
        done = _batch(partial)
        if done["stopped"] is None:
            done["stopped"] = code
        self._account(night, phase, done)
        return done

    def _account(self, night: _Night, phase: str, batch: dict[str, Any]) -> None:
        night.data["trials"] += batch["trials"]
        night.phase_used[phase] = night.phase_used.get(phase, 0) + batch["trials"]
        self._save(night, "running")
        if batch["unknown_effects"]:
            # IC-18: every budget root with an unknown effect (an ablation's derived proposals
            # run on their own roots, ``roots``), else the batch's root
            roots = [
                r for r in (batch.get("roots") or [batch.get("root") or batch.get("proposal_id")])
                if r is not None
            ]  # fmt: skip
            for root in roots:
                if root not in night.data["reconcile_pending"]:
                    night.data["reconcile_pending"].append(root)
            self._save(night, "running")
            raise _Stop("unknown_effect", {"root": roots[0] if roots else None})  # IC-18 (b)
        stopped = batch.get("stopped")
        if stopped in APPROVAL_CODES and self._night_reason(night) == "standing_approval":
            raise _Stop("standing_approval")
        if stopped == "KILL_SWITCH":
            raise _Stop("kill_switch")

    # -- phases -----------------------------------------------------------------------------------
    def _preflight(self, night: _Night) -> None:
        night.data["phase"] = "preflight"
        if self.backend.kill_switch():
            self._phase(night, "preflight", "stopped", reason="kill_switch")
            raise _Stop("kill_switch")
        problems = self.backend.preflight(self.config.cells)
        if float(self.clock()) >= night.stop_at:
            problems = [*problems, {"code": "STOP_AT", "stop_at": self.config.stop_at}]
        if problems:
            self._phase(night, "preflight", "stopped", problems=problems)
            raise _Stop("preflight", problems)
        self._phase(night, "preflight", "done",
                    stop_at=datetime.fromtimestamp(night.stop_at).astimezone().isoformat(),
                    )  # fmt: skip

    def _drift(self, night: _Night) -> None:
        results: dict[str, Any] = {}
        drifted: list[str] = []
        pending: set[str] = set()
        for cell in self.config.cells:
            check = self.drift_check(cell)
            since = self.recalibration_pending(cell, night.date, check.get("calibrated_at"))
            if since is not None:
                # an earlier night's drift waits for the operator's re-calibration: search stays
                # stopped and no drift trial runs against the stale baseline
                check["status"], check["recalibration_pending_since"] = (
                    "recalibration_pending",
                    since,
                )
                pending.add(cell)
            elif check["status"] != "changed" and self.config.drift_tasks and not night.dry_run:
                limit_each = self._remaining(night, "drift") // max(
                    1, len(self.config.cells) - len(results)
                )
                batch = self._unit(
                    night, "drift",
                    partial(self.backend.drift_run, cell, list(self.config.drift_tasks)),
                    f"DRIFT {cell}", cap=limit_each,
                )  # fmt: skip
                # None: the drift share could not cover it or a hold refused it (a finding)
                check["observed"] = None
                if batch is None and not any(
                    f.startswith(f"DRIFT {cell}:") for f in night.data["findings"]
                ):
                    night.data["findings"].append(f"DRIFT {cell}: PHASE_SHARE")
                if batch is not None and batch["runs"]:
                    check["observed"] = {"passes": batch["passes"], "runs": batch["runs"]}
                    band = check["band"]
                    if band is not None:
                        # the 99 % binomial prediction range at the ends of the calibrated
                        # Wilson interval, so a small n does not stop a night by chance
                        lower, upper = drift_range(int(batch["runs"]), band["wilson_95"])
                        check["prediction_99"] = [lower, upper]
                        passes = int(batch["passes"])
                        check["outside_band"] = not lower <= passes <= upper
                        if check["outside_band"]:
                            check["status"] = "outside_band"
            results[cell] = check
            if check["status"] in ("changed", "outside_band", "recalibration_pending"):
                drifted.append(cell)
        night.data["drift"] = results
        if drifted:
            night.search_blocked = True
            for cell in drifted:
                reason = f"drift {results[cell]['status']} ({night.date})"
                if night.dry_run:
                    night.data["recalibration"].append({"cell_id": cell, "dry_run": True})
                    continue
                if cell in pending:  # scheduled (and swept) by that earlier night already
                    night.data["recalibration"].append(
                        {
                            "cell_id": cell,
                            "pending_since": results[cell]["recalibration_pending_since"],
                        }
                    )
                    continue
                night.data["recalibration"].append(
                    {"cell_id": cell, **(self.backend.schedule_recalibration(cell, reason) or {})}
                )
                try:
                    night.data["proposals"] += self.backend.removal_sweep(cell, reason)
                except Hold as exc:
                    night.data["findings"].append(f"REMOVAL_SWEEP {cell}: {exc.code}")
            self._stopped(night, "drift", {"cells": drifted})
        self._phase(night, "drift", "drift" if drifted else "done", cells=sorted(results),
                    drifted=drifted)  # fmt: skip

    def _screening(self, night: _Night) -> None:
        name = "screening_design"
        if night.plan["shares"]["screening_design"] <= 0:
            self._phase(night, name, "skipped", reason="share_zero")
            return
        if night.search_blocked:
            self._phase(night, name, "skipped", reason="drift")
            return
        done = sum(
            1 for v in self._earlier_nights(night.date)
            for p in v.get("phases") or [] if p.get("phase") == name and p.get("state") == "done"
        )  # fmt: skip
        design = plackett_burman_12() if done % 2 == 0 else fold_over(plackett_burman_12())[12:]
        units = []
        for cell in self.config.cells:
            factors = list(self.backend.screening_factors(cell))[:PB12_FACTORS]
            configs = [
                {f: ("on" if row[i] > 0 else "off") for i, f in enumerate(factors)}
                for row in design
            ]
            unit: dict[str, Any] = {"cell_id": cell, "factors": factors, "runs": len(configs),
                                    "fold_over": done % 2 == 1}  # fmt: skip
            if not night.dry_run and factors:
                batch = self._unit(
                    night, name,
                    partial(self.backend.screening_run, cell, configs),
                    f"SCREENING_DESIGN {cell}",
                )  # fmt: skip
                unit["trials"] = batch["trials"] if batch is not None else 0
                if batch is not None:
                    unit.update({k: batch[k] for k in ("unbuilt", "rows") if k in batch})
            units.append(unit)
        self._phase(night, name, "planned" if night.dry_run else "done", design="PB12",
                    units=units)  # fmt: skip

    def _confirmations_due(self, night: _Night) -> list[dict[str, Any]]:
        if night.search_blocked:
            return []
        return list(self.backend.confirmations())

    def _search(self, night: _Night, *, extra: int) -> None:
        name = "search"
        if night.search_blocked:
            self._phase(night, name, "skipped", reason="drift")
            return
        night.caps[name] = night.caps[name] + extra  # §8.8: the confirmation share returns
        night.caps["confirmation"] -= extra
        units = []
        for cell in self.config.cells:
            drafted: list[str] = []
            if not night.dry_run:
                try:
                    drafted = list(self.backend.propose(cell))
                except Hold as exc:
                    night.data["findings"].append(f"PROPOSER {cell}: {exc.code}")
                night.data["proposals"] += drafted
            candidates = list(self.backend.search_candidates(cell))
            rows = self.backend.search_rows(cell)
            tasks = self.backend.development_tasks(cell)
            model = AdditiveSurrogate.fit(rows)
            ranked = model.rank([dict(c.get("options") or {}) for c in candidates], tasks)
            order = [str(candidates[i]["candidate_id"]) for i, _score in ranked]
            rungs = successive_halving(order) if order else []
            unit: dict[str, Any] = {
                "cell_id": cell, "drafted": drafted, "ranking": [
                    {"candidate_id": str(candidates[i]["candidate_id"]), "score": score}
                    for i, score in ranked
                ],
                "rungs": [{k: r[k] for k in ("rung", "tasks", "count")} for r in rungs],
                "queued": [],
            }  # fmt: skip
            if not night.dry_run and rungs:
                unit["queued"] = self._halving(night, cell, order, rungs, unit)
            units.append(unit)
            self._save(night, "running")
        self._phase(night, name, "planned" if night.dry_run else "done", units=units)

    def _halving(
        self,
        night: _Night,
        cell: str,
        order: list[str],
        rungs: list[dict[str, Any]],
        unit: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Successive halving: each rung's survivors are the best measured pass rates of the
        previous rung (ties by surrogate order); the last rung's promising survivors are queued
        for the operator's confirmation (IC-10)."""
        alive = list(order)
        measured: dict[str, tuple[int, int]] = {}
        results: list[dict[str, Any]] = []
        for rung in rungs:
            alive = sorted(
                alive,
                key=lambda cid: (-(measured[cid][0] / measured[cid][1]) if cid in measured
                                 and measured[cid][1] else 0.0, order.index(cid)),
            )[: rung["count"]]  # fmt: skip
            for cid in alive:
                batch = self._unit(
                    night, "search",
                    partial(self.backend.search_run, cell, cid, int(rung["tasks"])),
                    f"SEARCH {cell} {cid}",
                )  # fmt: skip
                if batch is None:
                    continue
                measured[cid] = (batch["passes"], batch["runs"])
                results.append({"rung": rung["rung"], "candidate_id": cid,
                                "trials": batch["trials"], "passes": batch["passes"],
                                "runs": batch["runs"],
                                "promising": bool(batch.get("promising"))})  # fmt: skip
                if batch.get("proposal_id") and batch["proposal_id"] not in night.data["proposals"]:
                    night.data["proposals"].append(batch["proposal_id"])
        unit["results"] = results
        last = rungs[-1]["rung"] if rungs else None
        queued = []
        for row in results:
            if row["rung"] == last and row["promising"]:
                try:  # §8.8: frozen for the operator, never approved or run by the night
                    item = self.backend.queue_confirmation(cell, row["candidate_id"])
                except Hold as exc:
                    night.data["findings"].append(
                        f"QUEUE_CONFIRMATION {cell} {row['candidate_id']}: {exc.code}"
                    )
                    continue
                if item is not None:
                    queued.append(item)
                    night.data["queued_confirmations"].append(item)
        return queued

    def _confirm(self, night: _Night, items: list[dict[str, Any]]) -> None:
        name = "confirmation"
        if night.search_blocked:
            self._phase(night, name, "skipped", reason="drift")
            return
        if not items:
            self._phase(night, name, "skipped", reason="none_approved")
            return
        ran = []
        for item in items:
            if night.dry_run:
                ran.append({"item": item, "dry_run": True})
                continue
            batch = self._unit(
                night, name,
                partial(self.backend.confirmation_run, item),
                "CONFIRMATION",
            )  # fmt: skip
            ran.append({"item": item, "trials": batch["trials"] if batch else 0})
        self._phase(night, name, "planned" if night.dry_run else "done", items=ran)

    def _dream(self, night: _Night) -> None:
        name = "dreaming"
        if night.dry_run:
            self._phase(night, name, "skipped", reason="dry_run")
            return
        if night.data["stopped"] in NO_DREAM:
            self._phase(night, name, "skipped", reason=night.data["stopped"])
            return
        refs = []
        for cell in self.config.cells:
            try:
                ref = self.backend.dream(cell, night.date)
            except Hold as exc:
                night.data["findings"].append(f"DREAM {cell}: {exc.code}")
                continue
            if ref is not None:
                refs.append({"cell_id": cell, "ref": ref})
                night.data["dreaming_ref"] = ref
        self._phase(night, name, "done", results=refs)

    def _dashboard(self, night: _Night) -> None:
        pages = self.backend.dashboard()
        if pages is None:
            self._phase(night, "dashboard", "skipped", reason="no_dashboard_module")
            return
        self._phase(night, "dashboard", "done", pages=list(pages))


def _check_date(date: Any) -> None:
    try:
        ok = (
            isinstance(date, str) and datetime.strptime(date, "%Y-%m-%d").date().isoformat() == date
        )
    except ValueError:
        ok = False
    if not ok:
        raise RuntimeFault("NIGHT_DATE", "The night is a YYYY-MM-DD date")


def composition_snapshot(store: Any, scope: Any, composition_ref: dict[str, Any]) -> dict[str, Any]:
    """The model snapshot of a composition (as the trial receipt's ``model_snapshot``, §2.10)."""
    composition = store.get(scope, "harness-composition", composition_ref)
    model = store.get(scope, "model-profile", composition["model_profile_ref"])
    driver = store.get(scope, "driver-capabilities", composition["driver_profile_ref"])
    _kind, environment = resolve_ref(store, scope, composition["sandbox_profile_ref"])
    return {
        "provider_model_id": model.get("provider_model_id"),
        "driver_version": driver.get("driver_version"),
        "image": environment.get("image"),
    }


# -- the meta deployment's backend ---------------------------------------------------------------
class LocalNightlyBackend:
    """``NightlyBackend`` on the meta deployment (``LocalMetaOps`` whose trial executor runs as the
    nightly identity). What it wires (clarification after S12, IC-10 mechanics, IC-29):

    - executor qualification (IC-29, provisional): before every unit the qualification of the
      cell is rebuilt from the newest ``executor-qualification`` record a human operator wrote
      for it (``LocalMetaOps.restore_qualification``); a cell without one stops the night at
      preflight (``QUALIFIED_EXECUTOR_REQUIRED`` with the cell), so a launchd night needs no
      extra arguments.
    - drift: a calibration of the champion on the regression corpus (``amplai-regression-v1``,
      every case, one repeat; ``CalibrationService`` pins every case of its splits) approved by
      ``issue_standing``; skipped when that is more than the drift share allows. The band and the
      calibrated snapshot come from the human operator's newest calibration **on the regression
      set** (``amplai meta calibrate --set regression``), never from a night's drift run.
    - search: each candidate (a screened, non-derived proposal of the cell whose screening has
      not run) is planned once (``StageRunner.plan``, the root budget = the standing policy's
      ``max_budget``) and advanced by a ``StageRunner`` whose approval issuer is the nightly
      identity's (``stages.standing_issuer``: ``issue_standing``, exploratory development
      experiments only). ``advance`` runs the screening stage and stops at the focused gate. A
      failed screening is recorded (stage ``failed``, finding ``SCREENING_FAILED``) and left for
      the operator; the night never screens a draft (``harness.review``), reviews or rejects.
      A candidate's measurement is its screening stage: successive halving re-ranks measured
      candidates without re-running them (one screening per proposal).
    - screening design (PB12): a factor is a changed component (slot and component) of a
      screened candidate of the cell; a design row runs only when a screened candidate whose
      change is exactly the row's "on" factors waits for its screening (the night never builds or
      screens a proposal); other rows are counted ``unbuilt``.
    - queued confirmations (IC-10): a candidate that passed screening and survived the last rung
      gets its focused experiment built (``StageRunner.queue_stage``, no approval) and listed;
      ``confirmations`` are the queued focused stages the operator approved
      (``amplai meta approve-stage P --stage focused --queue``), which ``confirmation_run`` runs
      (``StageRunner.run_queued``) and then, when the share allows, advances (the ablation,
      exploratory, under the standing approval). Holdout stays an operator in-process run.
    - dashboard: ``dashboard.build_feed`` on the open store and ``write_site`` into
      ``<runtime_root>/dashboard``.
    """

    def __init__(
        self,
        ops: Any,
        runner: NightlyRunner,
        *,
        proposer_run: Callable[[str], Any] | None = None,
        dream_turn: Callable[[str], Any] | None = None,
    ) -> None:
        self.ops, self.runner = ops, runner
        self.store, self.scope = ops.store, ops.scope
        self.proposer_run, self.dream_turn = proposer_run, dream_turn

    @classmethod
    def open(cls, dep: Any, runner: NightlyRunner) -> LocalNightlyBackend:
        """``LocalMetaOps`` on ``dep`` with the corpus v2 of ``meta.corpus_root`` and a trial
        executor that runs as the nightly identity (no publisher)."""
        from ..runtime.execution.loop import ExecutionLoop
        from ..runtime.execution.meta_ops import LocalMetaOps
        from ..runtime.meta_cli import trial_traces
        from ..runtime.meta_commands import corpus_root, load_corpus
        from .local_executor import LocalTrialExecutor

        corpus = load_corpus(corpus_root(dep, None))
        loop = ExecutionLoop(dep.service, dep.coordinator, publisher=None)
        # §9.1: nightly development trials capture traces as the operator's trial commands do
        executor = LocalTrialExecutor(
            dep.service, loop, dep.goals, runner.identity, corpus, traces=trial_traces(dep)
        )
        cell = runner.config.cells[0] if runner.config.cells else "codex-cli"
        return cls(LocalMetaOps(dep, corpus, executor, driver=cell), runner)

    # -- checks -----------------------------------------------------------------------------------
    def kill_switch(self) -> bool:
        try:
            return bool(self.store.head(self.scope, "runtime-control", "kill")["data"]["enabled"])
        except RuntimeFault as exc:
            if exc.code != "NOT_FOUND":
                raise
            return False

    def quota_signal(self, since: float) -> bool:
        """A rate-limit event or limit error observed since ``since`` (none is stored yet,
        §14 Q5: ``QuotaObserver`` counts 0)."""
        from datetime import UTC

        until = (
            datetime.fromtimestamp(float(self.store.clock()), UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
        for window in QuotaObserver(self.store, self.scope).observe(until=until):
            if window.window == "1h" and (window.rate_limit_events or window.limit_errors):
                return True
        return False

    def preflight(self, cells: tuple[str, ...]) -> list[dict[str, Any]]:
        from ..evaluation import versions
        from ..runtime.execution.meta_ops import qualification_record

        problems: list[dict[str, Any]] = []
        installed = self._installed()
        for cell in cells:
            if cell not in installed:
                problems.append({"code": "CELL_UNKNOWN", "cell_id": cell})
        if versions.current_version_ref(self.store, self.scope) is None:
            problems.append({"code": "EVALUATOR_UNQUALIFIED"})
        # IC-29: every cell of the night has a human operator's executor qualification record
        for cell in cells:
            if qualification_record(self.store, self.scope, cell) is None:
                problems.append({"code": "QUALIFIED_EXECUTOR_REQUIRED", "cell_id": cell})
        return problems

    def _qualify(self, cell_id: str) -> None:
        """IC-29: pin the cell's qualification from its newest human operator's record (Hold
        QUALIFIED_EXECUTOR_REQUIRED without one)."""
        if self.ops.restore_qualification(cell_id) is None:
            raise Hold("QUALIFIED_EXECUTOR_REQUIRED",
                       "No executor-qualification of a human operator for this cell (IC-29)",
                       details={"cell_id": cell_id})  # fmt: skip

    def _installed(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for app in self.ops.dep.service.apps.values():
            for cell, ref in app.compositions.items():
                out.setdefault(cell, ref)
        return out

    def _drift_compositions(self) -> dict[str, dict[str, Any]]:
        """The installed compositions of the app the loaded regression set names (§10.6: the
        Work 030 demo app); every installed app's when the loaded corpus has no regression task
        or names no single installed app."""
        try:
            return dict(self.ops.set_app("regression").compositions)
        except Hold:
            return self._installed()

    # -- drift ------------------------------------------------------------------------------------
    def _regression_corpus(self) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """(ref, value) of the newest frozen ``amplai-regression-v1`` corpus (the newest revision
        of its task index, ``meta_local.regression_corpus_refs``; the same refs
        ``LocalMetaApprovals.standing_kind`` classifies as drift), or None."""
        refs = regression_corpus_refs(self.store, self.scope)
        if not refs:
            return None
        return refs[-1], self.store.get(self.scope, "eval-corpus", refs[-1])

    def _human_approved(self, plan: dict[str, Any]) -> bool:
        """The calibration plan's approval is the human operator's own (not one the nightly
        identity derived under a standing approval, so never a night's drift run)."""
        try:
            approval = self.store.get(self.scope, APPROVAL_KIND, plan["approval_ref"])
        except (RuntimeFault, KeyError, TypeError):
            return False
        approver = approval.get("approved_by") or {}
        return approver.get("kind") == "human" and approval.get("standing_ref") is None

    def _calibration(self, cell_id: str, corpus_ref: dict[str, Any] | None) -> Any:
        """(plan, summary) of the newest **human-approved** calibration of ``cell_id`` on
        ``corpus_ref``, or None. A night's drift run is a calibration plan too (``drift_run``);
        it is never read back as the calibrated baseline, so drift never re-baselines itself:
        only the operator's re-calibration does (§8.8, ``plan.md`` §9)."""
        from ..evaluation import calibration

        if corpus_ref is None:
            return None
        found = []
        for _ref, summary in self.store.list_objects(self.scope, calibration.SUMMARY_KIND):
            if cell_id not in (summary.get("cells") or {}):
                continue
            try:
                plan = self.store.get(self.scope, calibration.PLAN_KIND, summary["plan_ref"])
            except (RuntimeFault, KeyError, TypeError):
                continue
            if plan.get("corpus_ref") != corpus_ref or not self._human_approved(plan):
                continue
            found.append((str(summary.get("summarized_at")), plan, summary))
        if not found:
            return None
        _at, plan, summary = max(found, key=lambda row: row[0])
        return plan, summary

    def snapshot(self, cell_id: str, drift_tasks: tuple[str, ...]) -> dict[str, Any]:
        """The champion's snapshot now and the baseline: the operator's newest calibration on
        the regression set (clarification after S12); none -> ``uncalibrated``."""
        installed = self._drift_compositions()
        current = (
            composition_snapshot(self.store, self.scope, installed[cell_id])
            if cell_id in installed else {}
        )  # fmt: skip
        calibrated = band = calibrated_at = None
        regression = self._regression_corpus()
        latest = self._calibration(cell_id, regression[0] if regression else None)
        if latest is not None:
            plan, summary = latest
            calibrated_at = summary.get("summarized_at")
            refs = (plan.get("composition_refs") or {}).get(cell_id) or []
            if refs:
                calibrated = composition_snapshot(self.store, self.scope, refs[0])
            rows = ((summary.get("cells") or {}).get(cell_id) or {}).get("tasks") or {}
            picked = [rows[t] for t in drift_tasks if isinstance(rows.get(t), dict)]
            if picked:
                band = {"passes": sum(int(r.get("passes") or 0) for r in picked),
                        "runs": sum(int(r.get("runs") or 0) for r in picked)}  # fmt: skip
        return {"current": current, "calibrated": calibrated, "band": band,
                "calibrated_at": calibrated_at}  # fmt: skip

    def drift_run(self, cell_id: str, tasks: list[str], guard: Guard, limit: int) -> dict[str, Any]:
        from ..evaluation import calibration
        from ..runtime.execution.meta_local import EXECUTOR_ID

        regression = self._regression_corpus()
        if regression is None:
            raise Hold("NO_DRIFT_CORPUS", "No frozen amplai-regression-v1 corpus")
        corpus_ref, corpus = regression
        case_ids = [c["case_id"] for c in corpus["cases"] if c["split"] in calibration.SPLITS]
        present = {c["split"] for c in corpus["cases"]}
        splits = [s for s in calibration.SPLITS if s in present]
        installed = self._drift_compositions()
        if cell_id not in installed:
            raise Hold("CELL_UNKNOWN", "Not an installed cell", details=[cell_id])
        if len(case_ids) > limit:
            raise Hold("NIGHT_STOPPED", "The drift calibration needs more than the drift share",
                       details={"reason": "phase_share", "needs": len(case_ids)})  # fmt: skip
        self._qualify(cell_id)
        policy = self.ops.local.evaluation.executor_policy
        standing = self.runner.approvals.standing(self.runner._standing_ref)["policy"]
        budget = dict(standing["max_budget"])
        plan: dict[str, Any] = {
            "schema": calibration.PLAN_SCHEMA,
            "scope": self.scope.wire(),
            "cells": [cell_id],
            "composition_refs": {cell_id: [installed[cell_id]]},
            "corpus_ref": corpus_ref,
            # the regression set is all validation (corpus_v2.for_set, §10.6)
            "splits": splits,
            "case_ids": case_ids,
            "initial_repeats": 1,
            "adaptive": {
                "max_repeats": 1,
                "rule": calibration.RULE,
                "borderline": list(calibration.INFORMATIVE),
            },
            "budget": budget,
            "max_parallel": min(self.runner.config.max_parallel, budget["max_parallel_works"]),
            "frozen_at": now(),
        }
        guard(len(case_ids))
        plan["approval_ref"] = self.runner.derive(plan)
        service = calibration.CalibrationService(
            self.store, self.ops.dep.contracts, self.ops.dep.artifacts,
            approval_check=self.runner.approvals.check, executor_id=EXECUTOR_ID,
            executor_policy=policy,
        )  # fmt: skip
        nightly = self.runner.identity
        plan_ref = service.freeze(nightly, plan)
        summary_ref = service.run(nightly, plan_ref, self.ops.executor,
                                  parallel=plan["max_parallel"])  # fmt: skip
        run = self.store.head(self.scope, calibration.RUN_KIND, plan_ref["id"])
        trials = [
            self.store.get(self.scope, calibration.TRIAL_KIND, r) for r in run["data"]["trial_refs"]
        ]
        summary = self.store.get(self.scope, calibration.SUMMARY_KIND, summary_ref)
        rows = summary["cells"][cell_id]["tasks"]
        picked = [rows[t] for t in tasks if isinstance(rows.get(t), dict)]
        return {
            "trials": len(trials),
            "passes": sum(int(r.get("passes") or 0) for r in picked),
            "runs": sum(int(r.get("runs") or 0) for r in picked),
            "unknown_effects": sum(int(t.get("unknown_effects") or 0) for t in trials),
            "stopped": run["data"].get("stop_reason"),
            "root": "calibration:" + plan_ref["id"],
            "summary_ref": summary_ref,
        }

    def schedule_recalibration(self, cell_id: str, reason: str) -> dict[str, Any]:
        """Recorded in the night (the operator re-calibrates the regression set)."""
        return {"scheduled": True, "reason": reason,
                "command": "amplai meta calibrate --set regression"}  # fmt: skip

    def removal_sweep(self, cell_id: str, reason: str) -> list[str]:
        from . import proposer

        return list(proposer.removal_sweep(self.ops, cell_id=cell_id, reason=reason))

    # -- the stage runner of the night ------------------------------------------------------------
    def _stages(self, proposal_id: str, cell_id: str | None = None) -> Any:
        """A ``StageRunner`` whose approvals the nightly identity derives under the night's
        standing approval (``issue_standing``)."""
        from .stages import standing_issuer

        issuer = standing_issuer(self.runner.identity, self.runner.derive)
        return self.ops.stage_runner(proposal_id, cell_id=cell_id, parallel=self._parallel(),
                                     issuer=issuer)  # fmt: skip

    def _parallel(self) -> int:
        standing = self.runner.approvals.standing(self.runner._standing_ref)["policy"]
        return max(1, min(self.runner.config.max_parallel,
                          int(standing["max_budget"]["max_parallel_works"]), 4))  # fmt: skip

    def _root_budget(self) -> dict[str, Any]:
        """A candidate's root budget (IC-16) on a night: the standing policy's ``max_budget``
        (``issue_standing`` refuses a plan above it field by field)."""
        standing = self.runner.approvals.standing(self.runner._standing_ref)["policy"]
        return dict(standing["max_budget"])

    def _need(self, runner: Any, proposal_id: str, limit: int, guard: Guard) -> int:
        need = int(runner.pending_trials(proposal_id))
        if need > limit:
            raise Hold("NIGHT_STOPPED", "The next stage needs more than the phase share",
                       details={"reason": "phase_share", "needs": need})  # fmt: skip
        guard(need)
        return need

    def _measured(self, proposal_id: str, stage: str) -> dict[str, Any]:
        """The batch of one stage's report: its trials, the candidate arm's passes and runs, the
        unknown effects, the stop reason of an aborted run (an approval or kill-switch code)."""
        from .stages import stage_findings, stage_status

        step = next(s for s in stage_status(self.store, self.scope, proposal_id)
                    if s.stage == stage)  # fmt: skip
        out: dict[str, Any] = {"trials": 0, "passes": 0, "runs": 0, "unknown_effects": 0,
                               "stopped": None, "root": proposal_id, "proposal_id": proposal_id,
                               "stage": stage, "state": step.state,
                               "promising": stage == "screening" and step.state == "passed",
                               "findings": stage_findings(self.store, self.scope,
                                                          proposal_id).get(stage, [])}  # fmt: skip
        if step.report_ref is None:
            return out
        report = self.store.get(self.scope, "eval-report", step.report_ref)
        trials = [self.store.get(self.scope, "eval-trial", r) for r in report["run_refs"]]
        candidate = [t for t in trials if t["arm"] == "candidate"]
        out.update(
            trials=len(trials),
            passes=sum(1 for t in candidate if t["success"] is True),
            runs=len(candidate),
            unknown_effects=sum(int(t.get("unknown_effects") or 0) for t in trials),
            report_ref=step.report_ref,
        )
        if report["verdict"] == "aborted":
            from ..evaluation.receipts import read_receipt

            analysis = read_receipt(
                self.ops.dep.artifacts.read(self.scope, report["analysis_artifact"], trusted=True)
            )
            codes = [r for r in analysis.get("reasons") or []
                     if r in APPROVAL_CODES or r == "KILL_SWITCH"]  # fmt: skip
            out["stopped"] = codes[0] if codes else "aborted"
        return out

    # -- screening design and search --------------------------------------------------------------
    def _candidates(self, cell_id: str) -> list[tuple[str, dict[str, Any]]]:
        """(proposal id, proposal) of the screened, non-derived proposals of the cell whose
        screening stage has not run."""
        from ..runtime.execution import releases
        from .stages import is_derived, stage_status

        installed = self.ops.app.compositions
        out = []
        for _ref, proposal in self.store.list_objects(self.scope, "harness-change-proposal"):
            pid = str(proposal.get("proposal_id"))
            try:
                if self.store.head(self.scope, "evolution", pid)["state"] != "screened":
                    continue
                cell = releases.pin_allowed(
                    self.store, self.scope, installed, proposal["baseline_ref"]
                )
                if cell != cell_id or is_derived(self.store, self.scope, proposal):
                    continue
                steps = {s.stage: s.state for s in stage_status(self.store, self.scope, pid)}
                if steps.get("screening") not in (None, "pending"):
                    continue
            except (RuntimeFault, KeyError, TypeError):
                continue
            out.append((pid, proposal))
        return sorted(out, key=lambda row: row[0])

    def _factors_of(self, proposal: dict[str, Any]) -> list[str]:
        """The changed components of a proposal as factor names ``<slot>=<id>@<revision>``."""
        manifests = self.ops.manifests
        changes = manifests.diff(
            manifests.of_composition(proposal["baseline_ref"]),
            manifests.of_composition(proposal["candidate_ref"]),
        )
        out = []
        for change in changes:
            to = getattr(change, "to", None)
            label = f"{to['id']}@{to['revision']}" if isinstance(to, dict) else "none"
            out.append(f"{change.slot}={label}")
        return sorted(out)

    def screening_factors(self, cell_id: str) -> list[str]:
        """Layer switches of the cell (§8.7): the changed components of its screened candidates
        that wait for their screening, in name order."""
        factors: set[str] = set()
        for _pid, proposal in self._candidates(cell_id):
            try:
                factors.update(self._factors_of(proposal))
            except (RuntimeFault, KeyError, TypeError):
                continue
        return sorted(factors)

    def screening_run(
        self, cell_id: str, configs: list[dict[str, str]], guard: Guard, limit: int
    ) -> dict[str, Any]:
        """Run each design row whose exact configuration is a waiting screened candidate (its
        screening stage, under the standing approval); count the other rows ``unbuilt``."""
        by_on: dict[frozenset[str], str] = {}
        for pid, proposal in self._candidates(cell_id):
            try:
                by_on.setdefault(frozenset(self._factors_of(proposal)), pid)
            except (RuntimeFault, KeyError, TypeError):
                continue
        total: dict[str, Any] = {"trials": 0, "passes": 0, "runs": 0, "unknown_effects": 0,
                                 "stopped": None, "root": None, "rows": [],
                                 "unbuilt": 0}  # fmt: skip
        for index, config in enumerate(configs):
            on = frozenset(f for f, v in config.items() if v == "on")
            found = by_on.get(on)
            if found is None:
                total["unbuilt"] += 1
                continue
            pid = found
            try:
                batch = self.search_run(cell_id, pid, 0, guard_after(guard, total["trials"]),
                                        limit - total["trials"])  # fmt: skip
            except Hold as exc:
                # a row refused (phase share, budget, a night stop, a stage Hold) ends the unit;
                # the rows that ran before it are the night's either way (``_unit`` accounts the
                # partial batch before it classifies the Hold)
                total["held"] = {"row": index, "proposal_id": pid, "code": exc.code}
                raise _with_partial(exc, total) from exc
            total["rows"].append({"row": index, "proposal_id": pid, "trials": batch["trials"],
                                  "passes": batch["passes"], "runs": batch["runs"]})  # fmt: skip
            for key in ("trials", "passes", "runs", "unknown_effects"):
                total[key] += batch[key]
            if batch["unknown_effects"] or batch["stopped"]:
                total["stopped"], total["root"] = batch["stopped"], batch["root"]
                break
        return total

    def propose(self, cell_id: str) -> list[str]:
        if self.proposer_run is None:
            return []
        ref = self.proposer_run(cell_id)
        try:
            _kind, value = resolve_ref(self.store, self.scope, ref)
        except (RuntimeFault, TypeError):
            return []
        return [str(p) for p in value.get("submitted") or [] if isinstance(p, str)]

    def _manifest_options(self, composition_ref: dict[str, Any]) -> dict[str, str]:
        manifest = self.ops.manifests.of_composition(composition_ref)
        return {
            slot: (f"{ref['id']}@{ref['revision']}" if isinstance(ref, dict) else "none")
            for slot, ref in manifest.flat().items()
        }

    def search_candidates(self, cell_id: str) -> list[dict[str, Any]]:
        """Screened, non-derived proposals of the cell whose screening stage has not run (drafts
        wait for the operator's screen: the nightly identity has no ``harness.review``)."""
        out = []
        for pid, proposal in self._candidates(cell_id):
            try:
                options = self._manifest_options(proposal["candidate_ref"])
            except (RuntimeFault, KeyError, TypeError):
                continue
            out.append({"candidate_id": pid, "options": options})
        return out

    def search_rows(self, cell_id: str) -> list[dict[str, Any]]:
        """Development ``trial-metrics`` rows of the cell (``proposer.development_rows``) with
        their composition's manifest options."""
        from .proposer import development_rows

        latest: dict[str, dict[str, Any]] = {}
        for ref, _value in self.store.list_objects(self.scope, "harness-composition"):
            if ref["id"] not in latest or ref["revision"] > latest[ref["id"]]["revision"]:
                latest[ref["id"]] = ref
        options: dict[str, dict[str, str] | None] = {}
        rows = []
        for row in development_rows(self.store, self.scope, cell_id):
            cid = row.get("composition_id")
            if not isinstance(cid, str) or cid not in latest:
                continue
            if cid not in options:
                try:
                    options[cid] = self._manifest_options(latest[cid])
                except (RuntimeFault, KeyError, TypeError):
                    options[cid] = None
            if options[cid] is None or not isinstance(row.get("success"), bool):
                continue
            rows.append({"task_id": str(row["task_id"]), "options": options[cid],
                         "success": row["success"]})  # fmt: skip
        return rows

    def development_tasks(self, cell_id: str) -> list[str]:
        refs = self.ops.frozen_corpus()
        corpus = self.store.get(self.scope, "eval-corpus", refs["corpus_ref"])
        return [c["case_id"] for c in corpus["cases"] if c["split"] == "development"]

    def search_run(
        self, cell_id: str, candidate_id: str, tasks: int, guard: Guard, limit: int
    ) -> dict[str, Any]:
        """One candidate's measurement: its screening stage, run once by ``StageRunner.advance``
        under the standing approval (planned first, root budget = the standing ``max_budget``);
        a candidate already screened is returned as measured with no new trial. ``tasks`` (the
        rung's task count) is recorded only: the stage plan fixes screening at <= 12 informative
        development tasks (§8.1)."""
        from .stages import stage_status

        steps = {s.stage: s.state for s in stage_status(self.store, self.scope, candidate_id)}
        if steps.get("screening") not in (None, "pending"):
            return {**self._measured(candidate_id, "screening"), "trials": 0}
        self._qualify(cell_id)
        runner = self._stages(candidate_id, cell_id)
        runner.plan(candidate_id, cell_id=cell_id, root_budget=self._root_budget())
        self._need(runner, candidate_id, limit, guard)
        try:
            runner.advance(candidate_id)
        except Hold:
            # an aborted stage still recorded its trials (e.g. a standing approval revoked
            # mid-run): report them; a hold before any trial propagates (the runner classifies)
            batch = self._measured(candidate_id, "screening")
            if not batch["trials"]:
                raise
            return {**batch, "rung_tasks": tasks}
        return {**self._measured(candidate_id, "screening"), "rung_tasks": tasks,
                "last_stop": runner.last_stop}  # fmt: skip

    def queue_confirmation(self, cell_id: str, candidate_id: str) -> dict[str, Any] | None:
        """IC-10: build the candidate's focused experiment for the operator's ``--queue``
        approval (``StageRunner.queue_stage``; no approval, nothing runs)."""
        runner = self._stages(candidate_id, cell_id)
        item = runner.queue_stage(candidate_id, "focused")
        if item is None:
            raise Hold(str((runner.last_stop or {}).get("code") or "META_TOKEN_BUDGET"),
                       "The root budget cannot cover the focused stage",
                       details=runner.last_stop)  # fmt: skip
        return {"cell_id": cell_id, **item}

    def confirmations(self) -> list[dict[str, Any]]:
        """The queued focused stages the operator approved (``--queue``) for the night's cells."""
        from .stages import PLAN_KIND, queued_stages

        out = []
        cells = set(self.runner.config.cells)
        for item in queued_stages(self.store, self.scope, "approved"):
            plans = [v for r, v in self.store.list_objects(self.scope, PLAN_KIND)
                     if r["id"] == "stageplan-" + item["proposal_id"]]  # fmt: skip
            cell = plans[-1]["cell_id"] if plans else None
            if cell in cells:
                out.append({"proposal_id": item["proposal_id"], "stage": item["stage"],
                            "queue_id": item["queue_id"], "cell_id": cell,
                            "trials": int(item["trials"]),
                            "subject_digest": item["subject_digest"]})  # fmt: skip
        return out

    def confirmation_run(self, item: dict[str, Any], guard: Guard, limit: int) -> dict[str, Any]:
        """Run one operator-approved queued stage (``StageRunner.run_queued``), then, when the
        share and the budget allow, ``advance`` (the ablation, exploratory, under the standing
        approval; it stops at the holdout gate, which stays the operator's)."""
        from .stages import stage_status

        cell, pid = str(item["cell_id"]), str(item["proposal_id"])
        self._qualify(cell)
        need = int(item["trials"])
        if need > limit:
            raise Hold("NIGHT_STOPPED", "The confirmation needs more than its share",
                       details={"reason": "phase_share", "needs": need})  # fmt: skip
        guard(need)
        runner = self._stages(pid, cell)
        try:
            with self._standing_guard():
                runner.run_queued(pid, str(item["stage"]))
        except Hold:
            batch = self._measured(pid, str(item["stage"]))
            if not batch["trials"]:
                raise
            return batch
        batch = self._measured(pid, str(item["stage"]))
        batch["ablation"] = None
        if batch["stopped"] or batch["unknown_effects"]:
            return batch
        more = int(runner.pending_trials(pid))
        if more and batch["trials"] + more <= limit:
            try:
                guard(batch["trials"] + more)
            except Hold as exc:
                details = exc.details if isinstance(exc.details, dict) else {}
                batch["ablation"] = "skipped: " + str(details.get("reason"))
                return batch
            try:
                runner.advance(pid)
            except Hold as exc:
                # the focused stage's trials and the ablation trials that ran are the night's
                # either way (``_unit`` accounts the partial batch before it classifies the Hold)
                self._add_ablation(batch, pid)
                batch["ablation"] = "held: " + exc.code
                raise _with_partial(exc, batch) from exc
            state = {s.stage: s.state for s in stage_status(self.store, self.scope, pid)}
            batch["ablation"] = state.get("ablation")
            self._add_ablation(batch, pid)
        elif more:
            batch["ablation"] = "skipped: phase_share"
        return batch

    @contextmanager
    def _standing_guard(self) -> Iterator[None]:
        """While the night runs an experiment the operator approved, every trial guard also
        re-checks the night's standing approval (§8.8: a revoked or expired standing approval
        ends the night), as it does for a derived approval (``LocalMetaApprovals.check``)."""
        evaluation = self.ops.local.evaluation
        original = evaluation.approval_check
        approvals, standing_ref = self.runner.approvals, self.runner._standing_ref

        def check(scope: Any, ref: dict[str, Any], action: str, subject: str) -> Any:
            approvals.standing(standing_ref)  # Hold STANDING_APPROVAL
            return original(scope, ref, action, subject)

        evaluation.approval_check = check
        try:
            yield
        finally:
            evaluation.approval_check = original

    def _add_ablation(self, batch: dict[str, Any], proposal_id: str) -> None:
        """Add the derived proposals' ablation experiments to ``batch``: their trials and unknown
        effects, the roots with an unknown effect (``roots``: each variant runs on its own budget
        root, IC-18) and, when ``batch`` names no stop yet, the first approval or kill-switch
        code that stopped a variant. The ablation never gates (§8.1), so ``advance`` returns
        normally after a variant refused by a revoked standing approval; the code is how that
        stop reaches the night (§8.8)."""
        from .stages import RUN_KIND

        try:
            run = self.store.head(self.scope, RUN_KIND, "stagerun-" + proposal_id)
        except RuntimeFault:
            return
        roots: list[str] = []
        for derived in run["data"]["stages"].get("ablation", {}).get("ablation_proposals", []):
            try:
                measured = self._measured(derived, "ablation")
            except (RuntimeFault, StopIteration):
                continue
            batch["trials"] += int(measured["trials"])
            batch["unknown_effects"] += int(measured["unknown_effects"])
            if measured["unknown_effects"]:
                roots.append(str(derived))
            codes = [measured["stopped"], *measured["findings"]]
            stop = next((c for c in codes if c in APPROVAL_CODES or c == "KILL_SWITCH"), None)
            if stop is not None and not batch.get("stopped"):
                batch["stopped"] = stop
        if roots:
            batch["roots"] = roots

    # -- after the night --------------------------------------------------------------------------
    def dream(self, cell_id: str, night: str) -> dict[str, Any] | None:
        from . import proposer

        if self.dream_turn is None:
            return None
        proposal_id = proposer.dream(self.ops, cell_id=cell_id, night=night,
                                     turn=self.dream_turn(cell_id))  # fmt: skip
        return {"proposal_id": proposal_id} if proposal_id else None

    def dashboard(self) -> list[str] | None:
        """§8.10: the pages under ``<runtime_root>/dashboard``, built read-only from the store
        the runner already holds; ``nightly`` = ``{budget_trials, keep_operator_share}``."""
        from . import dashboard

        dep = self.ops.dep
        root = dep.local(dep.config.runtime_root)
        cfg = self.runner.config
        nightly = {"budget_trials": cfg.budget_trials,
                   "keep_operator_share": cfg.keep_operator_share}  # fmt: skip
        feed = dashboard.build_feed(self.store, self.scope, nightly=nightly)
        return [str(p) for p in dashboard.write_site(feed, root / "dashboard")]


def _with_partial(exc: Hold, batch: dict[str, Any]) -> Hold:
    """``exc`` again, its details carrying ``partial`` = the batch the unit ran before it."""
    details = dict(exc.details) if isinstance(exc.details, dict) else {"details": exc.details}
    return Hold(exc.code, exc.message, details={**details, "partial": dict(batch)})


def guard_after(guard: Guard, spent: int) -> Guard:
    """A guard for the rest of a unit that already dispatched ``spent`` trials."""
    return lambda trials: guard(spent + trials)


def policy_digest(policy: dict[str, Any]) -> str:
    """The subject digest a standing approval binds (§8.8)."""
    return digest(policy)


__all__ = [
    "NIGHTLY_ACTION",
    "LocalNightlyBackend",
    "NightlyBackend",
    "NightlyConfig",
    "NightlyRunner",
    "nightly_policy",
    "phase_budget",
    "stop_epoch",
]
