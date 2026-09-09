#!/usr/bin/env python3
"""Project Store edge adapter for durable Slack Work-status projection.

This file is intentionally a host adapter: source domain code never imports the
Loop Kit or reaches into Markdown/Project Store paths directly.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from amplai_runtime import ProjectStore, read_jsonl
from pydantic import SecretStr

from amplai_foundry.governance.slack_http import HttpSlackTransport
from amplai_foundry.governance.work_status_projection import (
    SlackApiWorkStatusTransport,
    SlackWorkStatusTransport,
    WorkStatusOutbox,
    WorkStatusProjection,
)

_STATUS_EVENTS = frozenset(
    {
        "work.claimed",
        "work.started",
        "work.completed",
        "work.blocked",
        "work.failed",
        "work.human_required",
        "work.waiting",
        "work.activated",
    }
)
_SAFE_VERIFIER = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_SAFE_VERDICTS = frozenset({"PASS", "FAIL", "WARN", "ACCEPT", "REJECT", "ESCALATE"})


class WorkStatusProjectionAdapter:
    """Copy authoritative Work snapshots into the outbox once per event hash."""

    def __init__(self, store: ProjectStore, outbox: WorkStatusOutbox) -> None:
        self.store = store
        self.outbox = outbox

    def sync(self) -> int:
        queued = 0
        self.store.verify_event_chain()
        latest_events = {}
        for event in read_jsonl(self.store.events_path):
            if event.get("event_type") not in _STATUS_EVENTS or event.get("entity_type") != "work":
                continue
            latest_events[event["entity_id"]] = event
        for event in latest_events.values():
            work = self.store.get_work(event["entity_id"])
            result = work.get("result") or {}
            evidence_refs = tuple(result.get("evidence_refs") or work.get("evidence_refs") or ())
            projection = WorkStatusProjection(
                work_id=work["work_id"],
                state=work["status"],
                revision=int(event["sequence"]),
                request_ref=work.get("request_ref"),
                attempt=int(work.get("attempts") or 0),
                next_human_action=(
                    "answer_required_question"
                    if work["status"] == "HUMAN_REQUIRED"
                    else "review_failure"
                    if work["status"] == "FAILED"
                    else None
                ),
                evidence_refs=evidence_refs,
                verification_summary=self._verification_summary(evidence_refs),
            )
            before = self.outbox.pending_count()
            self.outbox.enqueue(projection)
            queued += int(self.outbox.pending_count() > before)
        return queued

    def _verification_summary(self, evidence_refs: tuple[str, ...]) -> str | None:
        """Return only explicit verifier/review verdicts, never free-form evidence text."""
        verdicts: list[str] = []
        for evidence_ref in evidence_refs:
            evidence = self.store.get_evidence(evidence_ref)
            metadata = evidence.get("metadata")
            if not isinstance(metadata, dict):
                continue
            verdict = metadata.get("verdict")
            if not isinstance(verdict, str):
                continue
            normalized = verdict.strip().upper()
            if normalized not in _SAFE_VERDICTS:
                continue
            if evidence.get("evidence_type") == "review":
                if normalized not in {"ACCEPT", "REJECT", "ESCALATE"}:
                    continue
                label = "independent review" if metadata.get("independent") is True else "review"
            elif (
                isinstance(metadata.get("verifier"), str)
                and _SAFE_VERIFIER.fullmatch(metadata["verifier"])
                and normalized in {"PASS", "FAIL", "WARN"}
            ):
                label = f"verifier {metadata['verifier']}"
            else:
                continue
            verdicts.append(f"{label}: {normalized}")
        if not verdicts:
            return None
        rendered = "; ".join(sorted(set(verdicts)))
        return rendered[:240]

    def deliver_next(self, transport: SlackWorkStatusTransport) -> WorkStatusProjection | None:
        delivery = self.outbox.deliver_next(transport)
        return None if delivery is None else delivery.projection


def main() -> int:
    parser = argparse.ArgumentParser(description="Project Store Work status to Slack")
    parser.add_argument("--project-store", required=True)
    parser.add_argument("--outbox", required=True)
    parser.add_argument("--slack-channel")
    parser.add_argument("--slack-app-id")
    parser.add_argument("--deliver", action="store_true")
    args = parser.parse_args()
    adapter = WorkStatusProjectionAdapter(
        ProjectStore(args.project_store), WorkStatusOutbox(Path(args.outbox))
    )
    queued = adapter.sync()
    delivered = 0
    if args.deliver:
        token = os.environ.get("AMPLAI_SLACK_BOT_TOKEN", "").strip()
        if not args.slack_channel or not args.slack_app_id or not token:
            parser.error(
                "--deliver requires --slack-channel, --slack-app-id, and AMPLAI_SLACK_BOT_TOKEN"
            )
        transport = SlackApiWorkStatusTransport(
            HttpSlackTransport(
                bot_token=SecretStr(token),
                timeout_seconds=10,
                max_history_pages=1,
                lease_seconds=60,
            ),
            channel_id=args.slack_channel,
            app_id=args.slack_app_id,
            max_history_pages=1,
        )
        while adapter.deliver_next(transport) is not None:
            delivered += 1
    print({"queued": queued, "delivered": delivered})
    return 0


if __name__ == "__main__":
    sys.exit(main())
