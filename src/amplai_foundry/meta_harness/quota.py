"""Quota observation without a quota API (Work 033 S12, interfaces.md §3.13, §8.6, §2.14).

The subscription limit windows and sizes are not observable (§14 Q6), so this module measures
proxies from what the store keeps:

- **runs and tokens** per driver: every ``run-record`` (latest revision per run) whose finish time
  (or start time while it runs) falls in a rolling observation window ending at ``until``; the
  driver is the run's ``driver_profile_ref`` record (``driver-capabilities.driver_id``); input and
  output tokens are the run's reported ``usage`` (unknown counts are counted as 0 and the run is
  still counted); cached input tokens come from the run's ``usage-detail`` record
  (``cached_input_tokens`` for Codex, ``cache_read_input_tokens`` for Claude,
  ``agent_drivers/protocol.py`` ``USAGE_DETAIL_FIELDS``).
- **rate-limit events and limit errors**: the stream normalizer counts Claude ``rate_limit_event``
  (``agent_drivers/protocol.py:221-224``) but no stored record carries that count, and the shape of
  a limit error is unknown (§14 Q5). Both counters are therefore 0 and ``first_signal_at`` is None
  until a later slice stores them; every ``quota-observation`` says so in ``signal_source``
  (``none_stored_v1``). Nothing here guesses a field name.

``observe`` reads; ``record`` writes one ``quota-observation`` per driver and night (id
``quota-<driver>-<YYYY-MM-DD>``; a new revision each call); ``headroom`` turns the pilot nights'
observations into the suggested nightly budget B (§8.6, ``plan.md`` §10.5): per driver the tokens
in the rolling 5 h window at the first limit signal, or "at least" the largest 5 h window observed
when no signal was seen, and B = floor(keep x headroom / median tokens per trial).
"""

from __future__ import annotations

import contextlib
import math
import re
from dataclasses import asdict, dataclass
from statistics import median
from typing import Any, Literal

from ..runtime.contracts.identity import now
from ..runtime.contracts.semantics import resolve_ref
from ..runtime.errors import RuntimeFault
from ..runtime.execution.meta_local import epoch_of
from ..runtime.storage.store import Scope, Store

KIND = "quota-observation"
SCHEMA = "amplai.quota-observation.v1"
SIGNAL_SOURCE = "none_stored_v1"  # §14 Q5: no stored rate-limit or limit-error count yet
WINDOWS: dict[str, int] = {"1h": 3600, "5h": 5 * 3600, "24h": 86400, "7d": 7 * 86400}
HEADROOM_WINDOW = "5h"  # §8.6
CACHED_FIELDS = ("cached_input_tokens", "cache_read_input_tokens")
NIGHT = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
DEFAULT_KEEP = 0.5  # keep_operator_share (§12.2)

WindowName = Literal["1h", "5h", "24h", "7d"]


@dataclass(frozen=True)
class QuotaWindow:
    driver_id: str
    window: WindowName
    runs: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    rate_limit_events: int
    limit_errors: int
    first_signal_at: str | None


def _count(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


class QuotaObserver:
    def __init__(self, store: Store, scope: Scope) -> None:
        self.store, self.scope = store, scope

    # -- reading ---------------------------------------------------------------------------------
    def _runs(self) -> list[dict[str, Any]]:
        latest: dict[str, tuple[int, dict[str, Any]]] = {}
        for ref, value in self.store.list_objects(self.scope, "run-record"):
            if ref["id"] not in latest or ref["revision"] > latest[ref["id"]][0]:
                latest[ref["id"]] = (ref["revision"], value)
        return [value for _rev, value in latest.values()]

    def _driver(self, ref: Any, cache: dict[str, str | None]) -> str | None:
        if not isinstance(ref, dict) or not isinstance(ref.get("id"), str):
            return None
        key = f"{ref['id']}@{ref.get('revision')}"
        if key not in cache:
            cache[key] = None
            with contextlib.suppress(RuntimeFault, KeyError, TypeError):
                _kind, value = resolve_ref(self.store, self.scope, ref)
                driver = value.get("driver_id")
                cache[key] = driver if isinstance(driver, str) and driver else None
        return cache[key]

    def _cached(self, usage: dict[str, Any]) -> int:
        ref = usage.get("source_ref")
        if not isinstance(ref, dict):
            return 0
        try:
            kind, detail = resolve_ref(self.store, self.scope, ref)
        except (RuntimeFault, KeyError, TypeError):
            return 0
        if kind != "usage-detail":
            return 0
        # a usage-detail record keeps the provider's counts under ``fields`` (worker._usage)
        nested = detail.get("fields")
        fields: dict[str, Any] = nested if isinstance(nested, dict) else detail
        return next((_count(fields[k]) for k in CACHED_FIELDS if type(fields.get(k)) is int), 0)

    def observe(self, *, until: str) -> list[QuotaWindow]:
        """Per driver (sorted) and window (1h, 5h, 24h, 7d): what ran in the window ending at
        ``until`` (an ISO-8601 UTC time)."""
        end = epoch_of(until)
        if end is None:
            raise RuntimeFault("QUOTA_UNTIL", "until is an ISO-8601 UTC time")
        drivers: dict[str, str | None] = {}
        seen: dict[str, list[tuple[float, dict[str, Any]]]] = {}
        for run in self._runs():
            at = epoch_of(run.get("finished_at")) or epoch_of(run.get("started_at"))
            if at is None or at > end or end - at > max(WINDOWS.values()):
                continue
            driver = self._driver(run.get("driver_profile_ref"), drivers)
            if driver is None:
                continue
            seen.setdefault(driver, []).append((at, run))
        out: list[QuotaWindow] = []
        for driver in sorted(seen):
            for name, seconds in WINDOWS.items():
                runs = [r for at, r in seen[driver] if end - at <= seconds]
                usages: list[dict[str, Any]] = [
                    r["usage"] if isinstance(r.get("usage"), dict) else {} for r in runs
                ]
                out.append(
                    QuotaWindow(
                        driver_id=driver,
                        window=name,  # type: ignore[arg-type]
                        runs=len(runs),
                        input_tokens=sum(_count(u.get("input_tokens")) for u in usages),
                        output_tokens=sum(_count(u.get("output_tokens")) for u in usages),
                        cached_input_tokens=sum(self._cached(u) for u in usages),
                        rate_limit_events=0,  # §14 Q5: not stored
                        limit_errors=0,  # §14 Q5: not stored
                        first_signal_at=None,
                    )
                )
        return out

    # -- writing ---------------------------------------------------------------------------------
    def record(self, night: str) -> list[dict[str, Any]]:
        """One ``quota-observation`` per observed driver for ``night`` (YYYY-MM-DD), observed up
        to now (the store clock)."""
        if not isinstance(night, str) or not NIGHT.fullmatch(night):
            raise RuntimeFault("QUOTA_NIGHT", "night is YYYY-MM-DD")
        from datetime import UTC, datetime

        until = (
            datetime.fromtimestamp(float(self.store.clock()), UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        windows = self.observe(until=until)
        refs: list[dict[str, Any]] = []
        for driver in sorted({w.driver_id for w in windows}):
            record_id = f"quota-{driver}-{night}"
            value = {
                "schema": SCHEMA,
                "scope": self.scope.wire(),
                "driver_id": driver,
                "night": night,
                "until": until,
                "windows": [asdict(w) for w in windows if w.driver_id == driver],
                "signal_source": SIGNAL_SOURCE,
                "created_at": now(),
            }
            revision = 1 + max(
                (r["revision"] for r, _v in self.store.list_objects(self.scope, KIND)
                 if r["id"] == record_id),
                default=0,
            )  # fmt: skip
            with self.store.tx() as db:
                refs.append(self.store.put(db, self.scope, KIND, record_id, revision, value))
        return refs

    def latest(self) -> dict[str, Any] | None:
        """The newest ``quota-observation`` ref (by ``created_at``, then id), or None."""
        found = [
            (str(v.get("created_at")), r["id"], r["revision"], r)
            for r, v in self.store.list_objects(self.scope, KIND)
        ]
        return max(found, key=lambda row: row[:3])[3] if found else None

    # -- the pilot (§8.6, plan.md §10.5) -------------------------------------------------------
    def _night_records(self, nights: list[str]) -> dict[str, list[dict[str, Any]]]:
        """driver -> the latest observation of each pilot night."""
        latest: dict[str, tuple[int, dict[str, Any]]] = {}
        for ref, value in self.store.list_objects(self.scope, KIND):
            if value.get("night") not in nights:
                continue
            if ref["id"] not in latest or ref["revision"] > latest[ref["id"]][0]:
                latest[ref["id"]] = (ref["revision"], value)
        out: dict[str, list[dict[str, Any]]] = {}
        for _rev, value in sorted(latest.values(), key=lambda row: str(row[1].get("night"))):
            out.setdefault(str(value.get("driver_id")), []).append(value)
        return out

    def _trial_tokens(self, records: list[dict[str, Any]], driver: str) -> list[int]:
        """Tokens (input + output, both known) of the stored trials of ``driver`` that finished
        inside the 24 h before each night's observation."""
        spans = [
            (end - WINDOWS["24h"], end)
            for end in (epoch_of(r.get("until")) for r in records)
            if end is not None
        ]
        cache: dict[str, str | None] = {}
        out = []
        for _ref, trial in self.store.list_objects(self.scope, "eval-trial"):
            at = epoch_of(trial.get("finished_at"))
            if at is None or not any(lo <= at <= hi for lo, hi in spans):
                continue
            tokens_in, tokens_out = trial.get("input_tokens"), trial.get("output_tokens")
            if type(tokens_in) is not int or type(tokens_out) is not int:
                continue
            composition = trial.get("composition_ref")
            if not isinstance(composition, dict):
                continue
            try:
                value = self.store.get(self.scope, "harness-composition", composition)
            except (RuntimeFault, KeyError, TypeError):
                continue
            if self._driver(value.get("driver_profile_ref"), cache) == driver:
                out.append(tokens_in + tokens_out)
        return out

    def headroom(self, pilot_nights: list[str], *, keep: float = DEFAULT_KEEP) -> dict[str, Any]:
        """Per driver: ``headroom_tokens`` (the 5 h window at the first limit signal, else the
        largest 5 h window observed, ``basis`` ``at_least_max_observed``), the median tokens per
        trial and the suggested B = floor(keep x headroom / median). The operator sets B in
        ``meta.nightly.budget_trials``; nothing here changes the config."""
        if not isinstance(pilot_nights, list) or not all(
            isinstance(n, str) and NIGHT.fullmatch(n) for n in pilot_nights
        ):
            raise RuntimeFault("QUOTA_NIGHT", "pilot nights are YYYY-MM-DD dates")
        if type(keep) not in (int, float) or not 0 <= keep <= 1:
            raise RuntimeFault("QUOTA_KEEP", "keep_operator_share is in [0, 1]")
        drivers: dict[str, Any] = {}
        for driver, records in self._night_records(pilot_nights).items():
            windows = [
                w for r in records for w in r.get("windows") or []
                if isinstance(w, dict) and w.get("window") == HEADROOM_WINDOW
            ]  # fmt: skip
            signalled = sorted(
                (w for w in windows if w.get("first_signal_at")),
                key=lambda w: str(w["first_signal_at"]),
            )
            if signalled:
                first = signalled[0]
                tokens = _count(first.get("input_tokens")) + _count(first.get("output_tokens"))
                basis = "first_signal"
            else:
                tokens = max(
                    (_count(w.get("input_tokens")) + _count(w.get("output_tokens"))
                     for w in windows),
                    default=0,
                )  # fmt: skip
                basis = "at_least_max_observed"
            per_trial = self._trial_tokens(records, driver)
            middle = median(per_trial) if per_trial else None
            suggested = math.floor(keep * tokens / middle) if middle else None
            drivers[driver] = {
                "headroom_tokens": tokens,
                "basis": basis,
                "nights": [r.get("night") for r in records],
                "median_tokens_per_trial": middle,
                "trials_measured": len(per_trial),
                "suggested_budget_trials": suggested,
            }
        suggestions = [d["suggested_budget_trials"] for d in drivers.values()]
        known = [s for s in suggestions if s is not None]
        return {
            "pilot_nights": list(pilot_nights),
            "keep_operator_share": keep,
            "signal_source": SIGNAL_SOURCE,
            "drivers": drivers,
            # the binding driver: the smallest suggestion; None while any driver has no trial
            "suggested_budget_trials": min(known)
            if known and len(known) == len(suggestions)
            else None,
        }
