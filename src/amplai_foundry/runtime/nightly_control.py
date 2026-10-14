"""Nightly loop control records (Work 034 S1d; spec N-1..N-9, OD-12, D-117; plan.md §3.8).

The front agent (an intake actor holding ``nightly.control``) and the human operator may only make
a night smaller: stop it, skip a date, tighten tonight's budget or stop time, withdraw what they
set, and (front agent, N-9) ask for tonight's night inside the standing approval. Nothing here
widens anything: a standing approval is issued and extended only by the human operator
(``amplai meta nightly approve``), and a request that would loosen a limit is ``Hold
NIGHT_CONTROL_WIDEN``.

Where things live (Q-9 = product store):

- The records are ``nightly-control`` objects in the **product** store (one immutable revision
  each, ``SCHEMA``), written by :class:`NightlyControlService` in one transaction with an event
  ``nightly.control_recorded`` (aggregate ``nightly-control``). Idempotency is the store's
  ``commands`` table, keyed by the actor mark and the caller's key (the S1a path).
- The night (meta deployment) reads them **read-only** (:class:`ControlReader`, ``Store(readonly=
  True)``) at preflight, between phases and at every trial guard; a record it cannot read or that
  is malformed stops the night (``control_unreadable``, fail closed).
- The service reads the meta deployment read-only (:class:`MetaView`): the standing approval, the
  night heads and ``meta.nightly``. It never opens the meta store for writing, so it works while a
  night holds that store (``ACTIVE_OWNER``).

Record (``plan.md`` §3.8)::

    {schema, scope, control_id, action: stop|skip|tighten|start|clear, dates: [YYYY-MM-DD],
     limits: {budget_trials?, stop_at?}, issued_by: <wire actor>, reason, at, clears}

Folding (:func:`fold`): a record binds the nights of its ``dates`` until a valid ``clear`` names
it. The strictest binding control wins: any stop or skip stops the night; the budget is the
minimum; each tightened ``stop_at`` (``HH:MM``, the night's local clock) can only end the night
earlier. A ``clear`` by the front agent removes only a record with its own mark; the human
operator's removes any.

The night date (:func:`night_date`): a night is named by the date of its scheduled start
(``meta.nightly.start_at``). With a start before noon, a moment belongs to the night whose start
follows it within twelve hours (23:00 on the 14th is the night of the 15th); with a start at or
after noon, to the night of the same date until noon of the next day. One date runs once
(``NIGHT_STATE``), so an early start by the front agent replaces that night, never adds one.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .contracts.authority import INTAKE_KIND, Actor
from .contracts.identity import digest, new_id, now
from .errors import Conflict, Hold, RuntimeFault
from .storage.store import Scope, Store

KIND = "nightly-control"
SCHEMA = "amplai.nightly-control.v1"
EVENT = "nightly.control_recorded"
PERMISSION = "nightly.control"
OPERATION = "nightly.control"
ACTIONS = ("stop", "skip", "tighten", "start", "clear")
LIMIT_KEYS = ("budget_trials", "stop_at")
# stop reasons the night records (``meta_harness/nightly.py`` HOLD_REASONS)
OPERATOR_STOP = "operator_stop"
FRONT_AGENT_STOP = "front_agent_stop"
SKIPPED = "skipped"
CONTROL_UNREADABLE = "control_unreadable"
STOP_REASONS = (OPERATOR_STOP, FRONT_AGENT_STOP, SKIPPED, CONTROL_UNREADABLE)
OPERATOR, FRONT_AGENT = "operator", "front_agent"
MAX_DATES = 7  # a skip or tighten names at most a week of nights, at most a week ahead
MAX_REASON = 4000
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_HHMM = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")
_MARK = re.compile(r"intake:[a-z][a-z0-9_-]{0,31}")
RECORD_FIELDS = frozenset(
    {"schema", "scope", "control_id", "action", "dates", "limits", "issued_by", "reason", "at",
     "clears"}
)  # fmt: skip


def _bad_record(why: str, details: object = None) -> RuntimeFault:
    return RuntimeFault("NIGHT_CONTROL_RECORD", why, details=details)


def minutes(value: Any) -> int | None:
    """``HH:MM`` as minutes after midnight, or None."""
    if not isinstance(value, str) or not _HHMM.fullmatch(value):
        return None
    return int(value[:2]) * 60 + int(value[3:])


def _day(value: Any) -> date_cls | None:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        return None
    try:
        return date_cls.fromisoformat(value)
    except ValueError:
        return None


def local(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch).astimezone()


def night_date(epoch: float, start_at: str) -> str:
    """The night a moment belongs to (module docstring): the date of its scheduled start."""
    start = minutes(start_at)
    if start is None:
        raise RuntimeFault("NIGHTLY_CONFIG", "start_at is HH:MM")
    moment = local(epoch)
    shifted = moment + timedelta(hours=12) if start < 720 else moment - timedelta(hours=12)
    return shifted.date().isoformat()


def scheduled_start(date: str, start_at: str) -> float:
    """The epoch of the scheduled start of the night ``date`` (local ``start_at`` on that date)."""
    day, start = _day(date), minutes(start_at)
    if day is None or start is None:
        raise RuntimeFault("NIGHTLY_CONFIG", "A night is YYYY-MM-DD and start_at is HH:MM")
    at = datetime(day.year, day.month, day.day, start // 60, start % 60).astimezone()
    return at.timestamp()


def in_window(epoch: float, window: tuple[str, str]) -> bool:
    """``epoch`` lies in the daily local window [start, end) (it may wrap past midnight)."""
    start, end = minutes(window[0]), minutes(window[1])
    if start is None or end is None or start == end:
        return False
    moment = local(epoch)
    tod = moment.hour * 60 + moment.minute
    return start <= tod < end if start < end else (tod >= start or tod < end)


def earlier(value: str, than: str, max_hours: float) -> bool:
    """``value`` comes before ``than`` on a night's clock: within ``max_hours`` (the longest
    night) before it. 08:00 is not earlier than 07:00; 23:00 is, for a night of 8+ hours."""
    a, b = minutes(value), minutes(than)
    if a is None or b is None:
        return False
    return 0 < (b - a) % 1440 < max_hours * 60


def issuer(wire: Any) -> str:
    """``operator`` (a human) or ``front_agent`` (an intake actor, marked ``intake:<adapter>``)."""
    if isinstance(wire, dict) and wire.get("kind") == "human":
        return OPERATOR
    if (
        isinstance(wire, dict)
        and wire.get("kind") == "service"
        and isinstance(wire.get("authn_context_ref"), str)
        and _MARK.fullmatch(wire["authn_context_ref"])
    ):
        return FRONT_AGENT
    raise _bad_record("A control is issued by the operator or a front agent")


def validate(value: Any, scope: Scope) -> dict[str, Any]:
    """A stored control record in the documented shape (RuntimeFault NIGHT_CONTROL_RECORD)."""
    if not isinstance(value, dict) or set(value) != RECORD_FIELDS:
        raise _bad_record("A control record has exactly the documented fields")
    if value["schema"] != SCHEMA or value["scope"] != scope.wire():
        raise _bad_record("Unknown control schema or another scope")
    if not isinstance(value["control_id"], str) or not value["control_id"]:
        raise _bad_record("A control names its id")
    action = value["action"]
    if action not in ACTIONS:
        raise _bad_record("Unknown control action", {"action": action})
    dates = value["dates"]
    if (
        not isinstance(dates, list)
        or not 1 <= len(dates) <= MAX_DATES
        or any(_day(d) is None for d in dates)
        or len(set(dates)) != len(dates)
    ):
        raise _bad_record("A control names one to seven distinct dates")
    limits = value["limits"]
    if not isinstance(limits, dict) or set(limits) - set(LIMIT_KEYS):
        raise _bad_record("limits holds only budget_trials and stop_at")
    if action == "tighten":
        if not limits:
            raise _bad_record("A tighten names a limit")
        budget = limits.get("budget_trials")
        if "budget_trials" in limits and (type(budget) is not int or budget < 0):
            raise _bad_record("budget_trials is a nonnegative integer")
        if "stop_at" in limits and minutes(limits["stop_at"]) is None:
            raise _bad_record("stop_at is HH:MM")
    elif limits:
        raise _bad_record("Only a tighten has limits")
    issuer(value["issued_by"])
    if not isinstance(value["reason"], str) or not 1 <= len(value["reason"]) <= MAX_REASON:
        raise _bad_record("A control says why")
    if not isinstance(value["at"], str) or not value["at"]:
        raise _bad_record("A control has a time")
    clears = value["clears"]
    if (action == "clear") != (isinstance(clears, str) and bool(clears)):
        raise _bad_record("A clear (and only a clear) names the control it removes")
    return value


def read_records(store: Store, scope: Scope) -> list[dict[str, Any]]:
    """Every control record of ``scope``, validated, oldest first."""
    rows = [validate(v, scope) for _r, v in store.list_objects(scope, KIND)]
    return sorted(rows, key=lambda v: (v["at"], v["control_id"]))


def _active(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The records no valid ``clear`` names. A clear counts only when it names a record that is
    not a clear and its issuer may clear that record (the operator: any; a front agent: its own)."""
    by_id = {r["control_id"]: r for r in records}
    cleared: set[str] = set()
    for r in records:
        if r["action"] != "clear":
            continue
        target = by_id.get(r["clears"])
        if target is None or target["action"] == "clear":
            continue
        if issuer(r["issued_by"]) == OPERATOR or _same_issuer(r["issued_by"], target["issued_by"]):
            cleared.add(target["control_id"])
    return [r for r in records if r["action"] != "clear" and r["control_id"] not in cleared]


def _same_issuer(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return all(a.get(k) == b.get(k) for k in ("subject_id", "kind", "authn_context_ref"))


@dataclass(frozen=True)
class NightControls:
    """What binds the night of one date (the strictest of the active controls)."""

    date: str
    stop: str | None = None  # operator_stop | front_agent_stop | skipped
    budget_trials: int | None = None
    stop_at: tuple[str, ...] = ()
    starts: tuple[dict[str, Any], ...] = ()
    held: bool = False  # a stop or skip binds the date
    operator_held: bool = False  # ... one of them by the human operator
    applied: tuple[str, ...] = ()  # the ids of the binding controls (starts excluded)
    records: tuple[dict[str, Any], ...] = field(default=(), repr=False)


def fold(records: list[dict[str, Any]], date: str) -> NightControls:
    """The controls binding the night ``date`` (module docstring)."""
    mine = [r for r in _active(records) if date in r["dates"]]
    stops = [r for r in mine if r["action"] == "stop"]
    skips = [r for r in mine if r["action"] == "skip"]
    tightens = [r for r in mine if r["action"] == "tighten"]
    reason = None
    if any(issuer(r["issued_by"]) == OPERATOR for r in stops):
        reason = OPERATOR_STOP
    elif stops:
        reason = FRONT_AGENT_STOP
    elif skips:
        reason = SKIPPED
    budgets = [r["limits"]["budget_trials"] for r in tightens if "budget_trials" in r["limits"]]
    return NightControls(
        date=date,
        stop=reason,
        budget_trials=min(budgets) if budgets else None,
        stop_at=tuple(r["limits"]["stop_at"] for r in tightens if "stop_at" in r["limits"]),
        starts=tuple(r for r in mine if r["action"] == "start"),
        held=bool(stops or skips),
        operator_held=any(issuer(r["issued_by"]) == OPERATOR for r in (*stops, *skips)),
        applied=tuple(r["control_id"] for r in (*stops, *skips, *tightens)),
        records=tuple(mine),
    )


class ControlReader:
    """The night's read-only view of the product store's control records (N-4). Every call opens
    the store read-only (no owner lock, so it works while the product server owns the store) and
    folds the records of one date. Anything that keeps it from reading them, a missing store or a
    malformed record included, is ``Hold NIGHT_CONTROL_UNREADABLE``: the night stops."""

    def __init__(self, runtime_root: str | Path, scope: Scope) -> None:
        self.root, self.scope = Path(runtime_root).expanduser(), scope

    def records(self) -> list[dict[str, Any]]:
        try:
            if not (self.root / "runtime.sqlite3").is_file():
                raise RuntimeFault("NOT_FOUND", "No product store at the control store root")
            with Store(self.root, readonly=True) as store:
                return read_records(store, self.scope)
        except Exception as exc:
            raise Hold(
                "NIGHT_CONTROL_UNREADABLE",
                "The nightly control records cannot be read",
                details={"code": getattr(exc, "code", type(exc).__name__)},
            ) from exc

    def __call__(self, date: str) -> NightControls:
        return fold(self.records(), date)


# -- the meta deployment, read-only ----------------------------------------------------------------
@dataclass(frozen=True)
class NightPolicy:
    """The meta deployment's ``meta.nightly`` the controls are checked against."""

    budget_trials: int
    cells: tuple[str, ...]
    shares: dict[str, float]
    stop_at: str
    max_hours: float
    start_at: str
    start_window: tuple[str, str] | None  # ``agent_start_window``; None: no front-agent start

    @classmethod
    def from_entry(cls, entry: Any) -> NightPolicy:
        window = getattr(entry, "agent_start_window", None)
        return cls(
            budget_trials=int(entry.budget_trials),
            cells=tuple(entry.cells),
            shares=dict(entry.shares),
            stop_at=str(entry.stop_at),
            max_hours=float(entry.max_hours),
            start_at=str(getattr(entry, "start_at", "01:00")),
            start_window=(window.start, window.end) if window is not None else None,
        )


@dataclass(frozen=True)
class MetaState:
    standing: dict[str, Any] | None  # the current standing approval's policy
    nights: dict[str, dict[str, Any]]  # date -> the night head (state, data)


class MetaView:
    """The meta deployment seen read-only: its store (``Store(readonly=True)``) and its
    ``meta.nightly``. Opening it takes no owner lock, so it works while a night runs."""

    def __init__(self, runtime_root: str | Path, scope: Scope, policy: NightPolicy) -> None:
        self.root, self.scope, self.policy = Path(runtime_root).expanduser(), scope, policy

    @classmethod
    def from_config(cls, config: Path) -> MetaView:
        from .local_deployment import LocalConfig

        path = Path(config).expanduser().absolute()
        try:
            cfg = LocalConfig.model_validate_json(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise Hold("NIGHT_CONTROL_META", "The meta deployment config is not readable") from exc
        nightly = cfg.meta.nightly if cfg.meta is not None else None
        if nightly is None:
            raise Hold("NIGHT_CONFIG", "The config has no meta.nightly: not a meta deployment")
        root = Path(cfg.runtime_root).expanduser()
        root = (root if root.is_absolute() else path.parent / root).absolute()
        return cls(root, Scope.parse(cfg.scope), NightPolicy.from_entry(nightly))

    def read(self, dates: list[str]) -> MetaState:
        """The current standing approval's policy and the night heads of ``dates``. A store that
        cannot be read is ``Hold NIGHT_CONTROL_META`` (the caller refuses rather than guesses)."""
        from .execution.meta_local import LocalMetaApprovals

        try:
            if not (self.root / "runtime.sqlite3").is_file():
                return MetaState(None, {})  # no meta store yet: no approval, no night
            with Store(self.root, readonly=True) as store:
                approvals = LocalMetaApprovals(store, self.scope)
                ref = approvals.current_standing()
                policy = approvals.standing(ref)["policy"] if ref is not None else None
                nights = {}
                for date in dates:
                    try:
                        nights[date] = dict(store.head(self.scope, "nightly-run", f"night-{date}"))
                    except RuntimeFault as exc:
                        if exc.code != "NOT_FOUND":
                            raise
                return MetaState(policy, nights)
        except Hold as exc:
            if exc.code == "NIGHT_CONTROL_META":
                raise
            raise Hold("NIGHT_CONTROL_META", "The meta deployment cannot be read") from exc
        except Exception as exc:
            raise Hold(
                "NIGHT_CONTROL_META",
                "The meta deployment cannot be read",
                details={"code": getattr(exc, "code", type(exc).__name__)},
            ) from exc

    def running(self) -> str | None:
        """The date of a night whose head is ``running`` (today's or the previous), or None."""
        today = date_cls.today()
        dates = [(today + timedelta(days=d)).isoformat() for d in (-1, 0, 1)]
        state = self.read(dates)
        for date in dates:
            if (state.nights.get(date) or {}).get("state") == "running":
                return date
        return None


def standing_covers(policy: dict[str, Any] | None, night: NightPolicy) -> str | None:
    """Why the current standing approval does not cover the configured night (None: it does).
    Mirrors ``NightlyRunner.plan_night``: budget, cells and shares within the policy."""
    if policy is None:
        return "no_standing_approval"
    if night.budget_trials > policy["budget_trials"]:
        return "budget_trials"
    if not night.cells or set(night.cells) - set(policy["cells"]):
        return "cells"
    if dict(night.shares) != dict(policy["shares"]):
        return "shares"
    return None


def start_refusal(
    *,
    now_epoch: float,
    date: str,
    policy: NightPolicy,
    meta: MetaState,
    controls: NightControls,
) -> str | None:
    """Why a start request for the night ``date`` has no effect now (N-9), or None. Checked when
    the request is taken and again by the tick right before it starts the night."""
    if policy.start_window is None:
        return "no_start_window"
    if not in_window(now_epoch, policy.start_window):
        return "outside_window"
    if date in meta.nights:
        return "already_ran"
    if controls.held:
        return "held"
    return standing_covers(meta.standing, policy)


# -- the authority service (product side) ---------------------------------------------------------
class NightlyControlService:
    """Takes control requests as the product Control Plane (N-3): checks the actor and the rules,
    then writes the record and its event in one transaction, idempotent per actor and key.

    ``meta`` is the read-only view of the meta deployment (None when none is configured: then only
    ``stop`` and ``skip``, which need nothing from it, are taken)."""

    def __init__(
        self,
        store: Store,
        scope: Scope,
        meta: MetaView | None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store, self.scope, self.meta, self.clock = store, scope, meta, clock

    def _meta(self) -> MetaView:
        if self.meta is None:
            raise Hold("NIGHT_CONTROL_META", "No meta deployment is configured to check against")
        return self.meta

    def _tonight(self) -> str:
        start_at = self.meta.policy.start_at if self.meta is not None else "01:00"
        return night_date(float(self.clock()), start_at)

    def _dates(self, action: str, dates: list[str] | None) -> list[str]:
        if action == "start":
            if dates not in (None, [self._tonight()]):
                raise Hold("NIGHT_START_REFUSED", "A start request is for tonight's night only",
                           details={"condition": "date", "tonight": self._tonight()})  # fmt: skip
            return [self._tonight()]
        if action == "stop" and not dates:
            running = None
            if self.meta is not None:
                try:
                    running = self.meta.running()
                except Hold:
                    running = None  # a stop never waits on the meta store
            return [running or self._tonight()]
        if not isinstance(dates, list) or not dates:
            raise RuntimeFault("NIGHT_CONTROL_INPUT", "Name the nights (dates) this applies to")
        days = [_day(d) for d in dates]
        if any(d is None for d in days) or len(set(dates)) != len(dates) or len(dates) > MAX_DATES:
            raise RuntimeFault("NIGHT_CONTROL_INPUT", "One to seven distinct YYYY-MM-DD dates")
        first = date_cls.fromisoformat(self._tonight())
        # a stop may also name the night before (a night still running past its date's noon)
        low = first - timedelta(days=1) if action == "stop" else first
        for day in days:
            if day is None or not low <= day <= first + timedelta(days=MAX_DATES):
                raise RuntimeFault(
                    "NIGHT_CONTROL_INPUT",
                    "A date is tonight's night or one of the next seven",
                    details={"tonight": first.isoformat()},
                )
        return list(dates)

    def request(
        self,
        actor: Actor,
        key: str,
        *,
        action: str,
        reason: str,
        dates: list[str] | None = None,
        limits: dict[str, Any] | None = None,
        clears: str | None = None,
    ) -> dict[str, Any]:
        """Record one control (module docstring); returns ``{control_ref, control_id, action,
        dates}``. Refusals: ``FORBIDDEN`` (permission, another's control), ``Hold
        NIGHT_CONTROL_WIDEN`` (a loosening), ``Hold NIGHT_START_REFUSED`` (N-9, with the
        failed ``condition``), ``Hold NIGHT_CONTROL_META`` (the meta deployment cannot be read to
        check a rule that needs it), ``NIGHT_CONTROL_INPUT``."""
        actor.require(PERMISSION)
        if actor.scope != self.scope:
            raise RuntimeFault("SCOPE_MISMATCH", "The actor is of another scope")
        if actor.kind not in ("human", INTAKE_KIND):
            raise RuntimeFault("FORBIDDEN", "Only the operator or a front agent controls the night")
        if action not in ACTIONS:
            raise RuntimeFault("NIGHT_CONTROL_INPUT", "Unknown control action")
        if not isinstance(reason, str) or not 1 <= len(reason) <= MAX_REASON:
            raise RuntimeFault("NIGHT_CONTROL_INPUT", "Say why (1 to 4000 characters)")
        limits = dict(limits or {})
        if action == "tighten":
            if not limits or set(limits) - set(LIMIT_KEYS):
                raise RuntimeFault("NIGHT_CONTROL_INPUT", "A tighten names a budget or stop time")
            budget = limits.get("budget_trials")
            if "budget_trials" in limits and (type(budget) is not int or budget < 0):
                raise RuntimeFault("NIGHT_CONTROL_INPUT", "budget_trials is a nonnegative integer")
            if "stop_at" in limits and minutes(limits["stop_at"]) is None:
                raise RuntimeFault("NIGHT_CONTROL_INPUT", "stop_at is HH:MM")
        elif limits:
            raise RuntimeFault("NIGHT_CONTROL_INPUT", "Only a tighten has limits")
        if (action == "clear") != (isinstance(clears, str) and bool(clears)):
            raise RuntimeFault("NIGHT_CONTROL_INPUT", "A clear (and only a clear) names a control")
        payload = {"action": action, "reason": reason, "dates": dates, "limits": limits,
                   "clears": clears}  # fmt: skip
        mark = actor.authn_context_ref + ":" + actor.subject_id

        # a replay returns the first result before any rule is checked again
        replay = self._replay(mark, key, payload)
        if replay is not None:
            return replay
        if action == "clear":
            return self._clear(actor, mark, key, payload, str(clears), reason)
        resolved = self._dates(action, dates)
        meta_state = None
        if action in ("tighten", "start"):
            meta_state = self._meta().read(resolved)

        def run(db: Any) -> dict[str, Any]:
            records = read_records(self.store, self.scope)
            if action == "tighten":
                assert meta_state is not None
                self._check_tighten(limits, resolved, records, meta_state)
            elif action == "start":
                assert meta_state is not None
                self._check_start(resolved[0], records, meta_state)
            return self._write(db, actor, action, resolved, limits, reason, None)

        return self.store.command(self.scope, mark, key, payload, run, operation=OPERATION)

    def _replay(self, mark: str, key: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """The result of an earlier request with this key (``Store.command``'s row), or None; the
        same key with another request is ``IDEMPOTENCY_CONFLICT``."""
        if not key or len(key) > 256:
            raise RuntimeFault("IDEMPOTENCY_REQUIRED", "A bounded idempotency key is required")
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT request_digest,result FROM commands WHERE tenant=? AND project=? "
                "AND actor=? AND operation=? AND key=?",
                (*self.scope.keys(), mark, OPERATION, key),
            ).fetchone()
        if row is None:
            return None
        if row[0] != digest(payload):
            raise Conflict("IDEMPOTENCY_CONFLICT", "The key was used for a different request")
        cached: dict[str, Any] = json.loads(row[1])
        return cached

    def _check_tighten(
        self,
        limits: dict[str, Any],
        dates: list[str],
        records: list[dict[str, Any]],
        meta: MetaState,
    ) -> None:
        """Only toward less (N-2, AC-N2): below the budget the night would otherwise have, and a
        stop time earlier than the one it would otherwise end at."""
        policy = self._meta().policy
        for date in dates:
            current = fold(records, date)
            if (meta.nights.get(date) or {}).get("state") not in (None, "running"):
                raise Hold("NIGHT_CONTROL_ENDED", "That night ended", details={"date": date})
            if "budget_trials" in limits:
                bases = [policy.budget_trials]
                if meta.standing is not None:
                    bases.append(int(meta.standing["budget_trials"]))
                if current.budget_trials is not None:
                    bases.append(current.budget_trials)
                if limits["budget_trials"] >= min(bases):
                    raise Hold(
                        "NIGHT_CONTROL_WIDEN",
                        "A tighten only lowers the night's budget",
                        details={"date": date, "limit": "budget_trials", "now": min(bases)},
                    )
            if "stop_at" in limits:
                for than in (policy.stop_at, *current.stop_at):
                    if not earlier(limits["stop_at"], than, policy.max_hours):
                        raise Hold(
                            "NIGHT_CONTROL_WIDEN",
                            "A tighten only brings the night's stop time earlier",
                            details={"date": date, "limit": "stop_at", "now": than},
                        )

    def _check_start(self, date: str, records: list[dict[str, Any]], meta: MetaState) -> None:
        controls = fold(records, date)
        why = start_refusal(
            now_epoch=float(self.clock()), date=date, policy=self._meta().policy, meta=meta,
            controls=controls,
        )  # fmt: skip
        if why is None and controls.starts:
            why = "already_requested"  # the frequency limit: one start per night
        if why is not None:
            raise Hold("NIGHT_START_REFUSED", "The start request has no effect (N-9)",
                       details={"condition": why, "date": date})  # fmt: skip

    def _clear(
        self, actor: Actor, mark: str, key: str, payload: dict[str, Any], target_id: str,
        reason: str,
    ) -> dict[str, Any]:  # fmt: skip
        records = read_records(self.store, self.scope)
        target = next((r for r in _active(records) if r["control_id"] == target_id), None)
        if target is None:
            raise RuntimeFault("NOT_FOUND", "No active control with that id")
        if actor.kind == INTAKE_KIND:
            if not _same_issuer(actor.wire(), target["issued_by"]):
                raise RuntimeFault("FORBIDDEN", "The front agent clears only its own controls")
            if target["action"] not in ("skip", "tighten", "start"):
                raise RuntimeFault("FORBIDDEN", "The front agent clears a skip, tighten or start")
            state = self._meta().read(list(target["dates"]))
            started = sorted(d for d in target["dates"] if d in state.nights)
            if started:
                raise Hold("NIGHT_CONTROL_STARTED", "Its night already started; it stays",
                           details={"dates": started})  # fmt: skip

        def run(db: Any) -> dict[str, Any]:
            again = read_records(self.store, self.scope)
            if not any(r["control_id"] == target_id for r in _active(again)):
                raise RuntimeFault("NOT_FOUND", "No active control with that id")
            return self._write(db, actor, "clear", list(target["dates"]), {}, reason, target_id)

        return self.store.command(self.scope, mark, key, payload, run, operation=OPERATION)

    def _write(
        self,
        db: Any,
        actor: Actor,
        action: str,
        dates: list[str],
        limits: dict[str, Any],
        reason: str,
        clears: str | None,
    ) -> dict[str, Any]:
        control_id = new_id("nightctl")
        value = {
            "schema": SCHEMA, "scope": self.scope.wire(), "control_id": control_id,
            "action": action, "dates": list(dates), "limits": dict(limits),
            "issued_by": actor.wire(), "reason": reason, "at": now(), "clears": clears,
        }  # fmt: skip
        validate(value, self.scope)
        ref = self.store.put(db, self.scope, KIND, control_id, 1, value)
        # the event carries no free text (H-8): ids, closed values, the actor mark
        self.store.event(
            db, self.scope, KIND, control_id, EVENT,
            {"control_id": control_id, "action": action, "dates": list(dates),
             "by": issuer(actor.wire()), "mark": actor.authn_context_ref, "clears": clears},
        )  # fmt: skip
        return {"control_ref": ref, "control_id": control_id, "action": action,
                "dates": list(dates)}  # fmt: skip


# -- the front agent's summary (N-5) ---------------------------------------------------------------
SUMMARY_SCHEMA = "amplai.night-summary.v1"
NIGHT_STATES = ("running", "stopped", "finished")


def summary(meta: MetaView, date: str, controls: list[dict[str, Any]] | None) -> dict[str, Any]:
    """The fixed-schema night summary for the front agent (N-5, H-8): read-only, closed values,
    counts and times; no plan text, finding, proposal or reason. ``controls`` are the product
    store's records (None: they could not be read, shown as ``controls_unreadable``)."""
    from ..meta_harness.nightly import HOLD_REASONS, PHASES

    state = meta.read([date])
    head = state.nights.get(date)
    night = None
    if head is not None:
        data: dict[str, Any] = head["data"] if isinstance(head.get("data"), dict) else {}
        phase, stopped = data.get("phase"), data.get("stopped")
        night = {
            "state": head.get("state") if head.get("state") in NIGHT_STATES else "unknown",
            "phase": phase if phase in PHASES else "unknown",
            "trials": data.get("trials") if type(data.get("trials")) is int else 0,
            "stopped": None if stopped is None else (
                stopped if stopped in HOLD_REASONS or stopped == "budget" else "other"
            ),
            "queued_confirmations": len(data.get("queued_confirmations") or []),
            "reconcile_pending": len(data.get("reconcile_pending") or []),
        }  # fmt: skip
    standing = state.standing
    out: dict[str, Any] = {
        "schema": SUMMARY_SCHEMA,
        "date": date,
        "standing": {
            "current": standing is not None,
            "valid_until": standing.get("valid_until") if standing is not None else None,
        },
        "night": night,
        "controls": None,
        "controls_unreadable": controls is None,
    }
    if controls is not None:
        out["controls"] = [
            {"control_id": r["control_id"], "action": r["action"], "by": issuer(r["issued_by"]),
             "at": r["at"]}
            for r in fold(controls, date).records
        ]  # fmt: skip
    return out
