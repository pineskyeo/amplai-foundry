"""What happened to each published draft PR (Work 019 R1, D-078).

The operator's authenticated ``gh`` reads the PR; nothing here writes to GitHub. Each transition
is recorded once, in the same transaction as the plan record, on the goal's audit trail:

- ``publication.merged`` — the PR was merged (final)
- ``publication.closed`` — closed without merging (final)
- ``publication.revised`` — the branch head is no longer the commit AMPLAI pushed: a human
  changed the result before deciding on it

A PR whose state cannot be read stays unknown and is read again at the next sync; it is never
counted as open, merged or closed on a guess.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..contracts.identity import now
from ..errors import Hold, RuntimeFault
from .product import PLAN_KIND, LocalExecutionService

FINAL = frozenset({"MERGED", "CLOSED"})


def gh_pr_state(repo: Path, url: str) -> dict[str, Any]:
    """Read one PR with the operator's ``gh`` (read-only)."""
    result = subprocess.run(
        ["gh", "pr", "view", url, "--json", "state,headRefOid,mergedAt,closedAt"],
        cwd=repo, capture_output=True, timeout=120, check=False,
    )  # fmt: skip
    if result.returncode != 0:
        raise Hold("PR_STATE", "gh pr view failed", details=result.stderr.decode()[-400:])
    value: dict[str, Any] = json.loads(result.stdout)
    return value


class PullRequestTracker:
    def __init__(
        self,
        service: LocalExecutionService,
        *,
        reader: Callable[[Path, str], dict[str, Any]] = gh_pr_state,
        clock: Callable[[], float] = time.time,
        interval_seconds: float = 600.0,
    ) -> None:
        self.service, self.reader, self.clock = service, reader, clock
        self.interval = interval_seconds
        self._last: float | None = None

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

    def sync(self) -> list[dict[str, Any]]:
        """Read every undecided PR once; return the transitions recorded."""
        self._last = self.clock()
        recorded = []
        for plan in self._pending():
            goal_id, publication = plan["goal_id"], plan["publication"]
            try:
                repo = self.service.apps[plan["app"]].config.repo
                state = self.reader(repo, publication["pr_url"])
            except (Hold, RuntimeFault, OSError, ValueError, KeyError):
                continue  # unknown stays unknown; retried at the next sync
            if state.get("state") not in {"OPEN", "MERGED", "CLOSED"}:
                continue
            before = plan.get("publication_outcome") or {}
            revised = bool(state.get("headRefOid")) and state["headRefOid"] != publication.get(
                "commit"
            )
            outcome = {
                "state": state["state"],
                "revised": revised or bool(before.get("revised")),
                "head": state.get("headRefOid"),
                "checked_at": now(),
            }
            transitions = []
            if outcome["revised"] and not before.get("revised"):
                transitions.append(("publication.revised", {"head": outcome["head"]}))
            if outcome["state"] in FINAL and before.get("state") != outcome["state"]:
                transitions.append(("publication." + outcome["state"].lower(), {}))
            current = {**self.service.plan_record(goal_id), "publication_outcome": outcome}
            self.service._save_plan(goal_id, current, transitions)
            recorded += [{"goal_id": goal_id, "event": t[0]} for t in transitions]
        return recorded
