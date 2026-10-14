"""What happened to each published draft PR (Work 019 R1, D-078).

The configured ``git_host`` module reads the PR (Work 034 OD-15, ``runtime/git_host/``; its v1
``pr_state`` is read-only); nothing here writes to the code host. Each transition is recorded
once, in the same transaction as the plan record, on the goal's audit trail:

- ``publication.merged`` — the PR was merged (final)
- ``publication.closed`` — closed without merging (final)
- ``publication.revised`` — the branch head is no longer the commit AMPLAI pushed: a human
  changed the result before deciding on it

A PR whose state cannot be read stays unknown and is read again at the next sync; it is never
counted as open, merged or closed on a guess.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..contracts.identity import now
from ..errors import Hold, RuntimeFault
from .product import PLAN_KIND, LocalExecutionService
from .publish import NO_GIT_HOST

if TYPE_CHECKING:
    from ..git_host import GitHost, PullRequestState

FINAL = frozenset({"MERGED", "CLOSED"})
OPEN_OR_FINAL = frozenset({"OPEN", "MERGED", "CLOSED"})


class PullRequestTracker:
    """Reads PRs through ``git_host.pr_state``; ``reader`` replaces it (test stand-ins). With
    neither, a sync holds with ``PUBLISH_NO_GIT_HOST``."""

    def __init__(
        self,
        service: LocalExecutionService,
        *,
        git_host: GitHost | None = None,
        reader: Callable[[Path, str], PullRequestState] | None = None,
        clock: Callable[[], float] = time.time,
        interval_seconds: float = 600.0,
    ) -> None:
        if reader is None and git_host is not None:
            reader = git_host.pr_state
        self.service, self.reader, self.clock = service, reader, clock
        self.interval = interval_seconds
        self._last: float | None = None
        # goal id -> the last coded refusal reading its PRs (cleared by a later read); shown by
        # ``ops pr-sync``, not stored (the stored ``publication_outcome`` shape is unchanged)
        self.unread: dict[str, dict[str, Any]] = {}

    def due(self) -> bool:
        return self._last is None or self.clock() - self._last >= self.interval

    def _pending(self) -> list[dict[str, Any]]:
        store, scope = self.service.store, self.service.scope
        with store._lock:
            rows = store.conn.execute(
                "SELECT data FROM heads WHERE tenant=? AND project=? AND kind=? AND state=?",
                (*scope.keys(), PLAN_KIND, "published"),
            ).fetchall()
        plans = [json.loads(r["data"]) for r in rows]
        return [
            p
            for p in plans
            if (p.get("publication") or {}).get("pr_url")
            and (p.get("publication_outcome") or {}).get("state") not in FINAL
        ]

    def _opened(self, goal_id: str) -> bool:
        store, scope = self.service.store, self.service.scope
        with store._lock:
            row = store.conn.execute(
                "SELECT 1 FROM events WHERE tenant=? AND project=? AND aggregate_id=? "
                "AND event_type='publication.opened' LIMIT 1",
                (*scope.keys(), goal_id),
            ).fetchone()
        return row is not None

    def sync(self) -> list[dict[str, Any]]:
        """Read every undecided PR once; return the transitions recorded."""
        reader = self.reader
        if reader is None:
            raise Hold(NO_GIT_HOST, "No git_host module is configured; PRs cannot be read")
        self._last = self.clock()
        recorded = []
        for plan in self._pending():
            goal_id, publication = plan["goal_id"], plan["publication"]
            # a multi-app goal has one PR per app (D-081); its outcome is the aggregate
            parts = publication.get("publications") or {plan["app"]: publication}
            states: dict[str, PullRequestState] = {}
            app = ""
            try:
                for app, part in parts.items():
                    if part.get("pr_url"):
                        repo = self.service.apps[app].config.repo
                        states[app] = reader(repo, part["pr_url"])
            except RuntimeFault as fault:  # Hold included
                # unknown stays unknown; retried at the next sync. A coded refusal (e.g.
                # GIT_HOST_MISMATCH) is kept for the operator, in memory only (not stored)
                self.unread[goal_id] = {
                    "goal_id": goal_id, "app": app, "code": fault.code,
                    "details": fault.details, "at": now(),
                }  # fmt: skip
                continue
            except (OSError, ValueError, KeyError):
                continue  # unknown stays unknown; retried at the next sync
            self.unread.pop(goal_id, None)
            if not states or any(s.state not in OPEN_OR_FINAL for s in states.values()):
                continue
            before = plan.get("publication_outcome") or {}
            kinds = {s.state for s in states.values()}
            revised_apps = [
                app
                for app, s in states.items()
                if s.head_commit and s.head_commit != parts[app].get("commit")
            ]
            aggregate = (
                "MERGED" if kinds == {"MERGED"} else "CLOSED" if "CLOSED" in kinds else "OPEN"
            )
            outcome: dict[str, Any] = {
                "state": aggregate,
                "revised": bool(revised_apps) or bool(before.get("revised")),
                "head": next(iter(states.values())).head_commit,
                "checked_at": now(),
                **(
                    {"parts": {a: {"state": s.state, "revised": a in revised_apps}
                               for a, s in states.items()}}
                    if len(parts) > 1
                    else {}
                ),
            }  # fmt: skip
            transitions = []
            if not self._opened(goal_id):
                # published before publication.opened existed: the PR exists (we just read it)
                transitions.append(
                    ("publication.opened", {"pr_url": publication["pr_url"], "backfilled": True})
                )
            if outcome["revised"] and not before.get("revised"):
                transitions.append(("publication.revised", {"head": outcome["head"]}))
            if outcome["state"] in FINAL and before.get("state") != outcome["state"]:
                transitions.append(("publication." + aggregate.lower(), {}))
            current = {**self.service.plan_record(goal_id), "publication_outcome": outcome}
            self.service._save_plan(goal_id, current, transitions)
            recorded += [{"goal_id": goal_id, "event": t[0]} for t in transitions]
        return recorded
