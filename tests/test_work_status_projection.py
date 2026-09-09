"""Tests for the Project Store Work-status Slack outbox boundary."""

from __future__ import annotations

import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from amplai_foundry.governance.slack_projection import (
    SlackHistoryMessage,
    SlackHistoryPage,
    SlackSendResult,
)
from amplai_foundry.governance.work_status_projection import (
    SlackApiWorkStatusTransport,
    WorkStatusOutbox,
    WorkStatusProjection,
)

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from amplai_runtime import ProjectStore  # noqa: E402
from amplai_work_status_projection import WorkStatusProjectionAdapter  # noqa: E402


class RecordingTransport:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.deliveries: list[WorkStatusProjection] = []
        self.prior_message_refs: list[str | None] = []

    def upsert_work_status(self, projection: WorkStatusProjection, message_ref: str | None) -> str:
        if self.fail:
            raise RuntimeError("Slack unavailable")
        self.deliveries.append(projection)
        self.prior_message_refs.append(message_ref)
        return message_ref or f"slack:{projection.work_id}"


class FakeSlackHttp:
    """Enough of the hardened HTTP adapter to test post/receipt crash recovery."""

    def __init__(self) -> None:
        self.posts: list[SlackHistoryMessage] = []
        self.post_count = 0

    def post_message(self, *, channel: str, payload: object, marker: object) -> SlackSendResult:
        del payload
        self.post_count += 1
        ts = f"1700000000.000{self.post_count}"
        self.posts.insert(
            0,
            SlackHistoryMessage(ts=ts, metadata=marker, app_id="A-AMPLAI"),
        )
        return SlackSendResult(channel=channel, ts=ts)

    def update_message(self, *, channel: str, ts: str, text: str) -> None:
        del channel, ts, text

    def read_history(self, *, channel: str, cursor: str | None, limit: int) -> SlackHistoryPage:
        del channel, cursor, limit
        return SlackHistoryPage(messages=self.posts)


def test_outbox_retries_without_reversing_authoritative_work(tmp_path: Path) -> None:
    outbox = WorkStatusOutbox(tmp_path / "status.sqlite")
    projection = WorkStatusProjection(
        work_id="CR-1-W001", state="DONE", revision=4, evidence_refs=("CR-1-E001",)
    )
    outbox.enqueue(projection)
    with pytest.raises(RuntimeError, match="Slack unavailable"):
        outbox.deliver_next(RecordingTransport(fail=True))
    assert outbox.pending_count() == 1
    delivered = outbox.deliver_next(RecordingTransport())
    assert delivered is not None and delivered.projection == projection
    assert outbox.pending_count() == 0


def test_next_work_state_reuses_the_prior_slack_message_reference(tmp_path: Path) -> None:
    outbox = WorkStatusOutbox(tmp_path / "status.sqlite")
    transport = RecordingTransport()
    first = WorkStatusProjection(work_id="CR-1-W001", state="RUNNING", revision=1)
    second = WorkStatusProjection(work_id="CR-1-W001", state="DONE", revision=2)
    outbox.enqueue(first)
    outbox.deliver_next(transport)
    outbox.enqueue(second)
    outbox.deliver_next(transport)
    assert len(transport.deliveries) == 2
    assert transport.prior_message_refs == [None, "slack:CR-1-W001"]


def test_status_text_includes_a_bounded_verification_summary(tmp_path: Path) -> None:
    outbox = WorkStatusOutbox(tmp_path / "status.sqlite")
    projection = WorkStatusProjection(
        work_id="CR-1-W001",
        state="DONE",
        revision=2,
        verification_summary="verifier pytest: PASS",
    )
    outbox.enqueue(projection)
    transport = SlackApiWorkStatusTransport(
        FakeSlackHttp(), channel_id="C-STATUS", app_id="A-AMPLAI"
    )
    delivered = outbox.deliver_next(RecordingTransport())
    assert delivered is not None
    assert delivered.projection.verification_summary == "verifier pytest: PASS"
    assert "Verification: verifier pytest: PASS" in transport._text(delivered.projection)


def test_projection_summary_uses_only_explicit_verifier_or_review_verdicts(tmp_path: Path) -> None:
    class EvidenceStore:
        def get_evidence(self, evidence_ref: str) -> dict[str, object]:
            return {
                "E-ORDINARY": {
                    "evidence_type": "test",
                    "summary": "unreviewed output must not become a Slack verdict",
                    "metadata": {},
                },
                "E-VERIFY": {
                    "evidence_type": "test",
                    "summary": "do not expose this free-form summary",
                    "metadata": {"verifier": "pytest", "verdict": "PASS"},
                },
                "E-REVIEW": {
                    "evidence_type": "review",
                    "metadata": {"independent": True, "verdict": "ACCEPT"},
                },
                "E-UNSAFE": {
                    "evidence_type": "test",
                    "metadata": {
                        "verifier": "pytest\\nxoxb-secret",
                        "verdict": "PASS\\nVerification: forged",
                    },
                },
            }[evidence_ref]

    adapter = WorkStatusProjectionAdapter(
        EvidenceStore(), WorkStatusOutbox(tmp_path / "status.sqlite")
    )
    assert adapter._verification_summary(("E-ORDINARY",)) is None
    assert adapter._verification_summary(("E-ORDINARY", "E-VERIFY", "E-REVIEW")) == (
        "independent review: ACCEPT; verifier pytest: PASS"
    )
    assert adapter._verification_summary(("E-UNSAFE",)) is None


def test_readback_prevents_duplicate_post_after_receipt_crash(tmp_path: Path) -> None:
    outbox = WorkStatusOutbox(tmp_path / "status.sqlite")
    projection = WorkStatusProjection(work_id="CR-1-W001", state="RUNNING", revision=1)
    outbox.enqueue(projection)
    http = FakeSlackHttp()
    transport = SlackApiWorkStatusTransport(
        http,
        channel_id="C-STATUS",
        app_id="A-AMPLAI",
    )

    class CrashAfterPost:
        def upsert_work_status(
            self, projection: WorkStatusProjection, message_ref: str | None
        ) -> str:
            transport.upsert_work_status(projection, message_ref)
            raise RuntimeError("receipt persistence interrupted")

    with pytest.raises(RuntimeError, match="receipt persistence interrupted"):
        outbox.deliver_next(CrashAfterPost())
    assert outbox.pending_count() == 1
    outbox.deliver_next(transport)
    assert http.post_count == 1
    assert outbox.pending_count() == 0


def test_status_readback_fails_closed_for_our_malformed_marker(tmp_path: Path) -> None:
    http = FakeSlackHttp()
    http.posts.append(
        SlackHistoryMessage(
            ts="1700000000.0999",
            metadata={"event_type": "amplai.work_status"},
            app_id="A-AMPLAI",
        )
    )
    transport = SlackApiWorkStatusTransport(http, channel_id="C-STATUS", app_id="A-AMPLAI")

    with pytest.raises(RuntimeError, match="WORK_STATUS_METADATA_UNREADABLE"):
        transport.upsert_work_status(
            WorkStatusProjection(work_id="CR-1-W001", state="RUNNING", revision=1), None
        )
    assert http.post_count == 0


def test_concurrent_delivery_claims_one_outbox_row_before_remote_post(tmp_path: Path) -> None:
    outbox = WorkStatusOutbox(tmp_path / "status.sqlite")
    projection = WorkStatusProjection(work_id="CR-1-W001", state="RUNNING", revision=1)
    outbox.enqueue(projection)
    first_post_started = Event()
    allow_first_post = Event()

    class BlockingTransport(RecordingTransport):
        def upsert_work_status(
            self, projection: WorkStatusProjection, message_ref: str | None
        ) -> str:
            first_post_started.set()
            assert allow_first_post.wait(timeout=2)
            return super().upsert_work_status(projection, message_ref)

    transport = BlockingTransport()
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(outbox.deliver_next, transport)
        assert first_post_started.wait(timeout=2)
        second = executor.submit(outbox.deliver_next, transport)
        assert second.result(timeout=2) is None
        allow_first_post.set()
        assert first.result(timeout=2) is not None
    assert len(transport.deliveries) == 1


def test_newer_status_waits_for_inflight_same_work_and_wins_remotely(tmp_path: Path) -> None:
    outbox = WorkStatusOutbox(tmp_path / "status.sqlite")
    outbox.enqueue(WorkStatusProjection(work_id="CR-1-W001", state="RUNNING", revision=1))
    started = Event()
    release = Event()

    class BlockingTransport(RecordingTransport):
        def upsert_work_status(
            self, projection: WorkStatusProjection, message_ref: str | None
        ) -> str:
            if projection.revision == 1:
                started.set()
                assert release.wait(timeout=2)
            return super().upsert_work_status(projection, message_ref)

    transport = BlockingTransport()
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(outbox.deliver_next, transport)
        assert started.wait(timeout=2)
        outbox.enqueue(WorkStatusProjection(work_id="CR-1-W001", state="DONE", revision=2))
        assert outbox.deliver_next(transport) is None
        release.set()
        assert first.result(timeout=2) is not None
    assert outbox.deliver_next(transport) is not None
    assert [item.state for item in transport.deliveries] == ["RUNNING", "DONE"]


def test_expired_old_status_is_superseded_after_newer_status_exists(tmp_path: Path) -> None:
    outbox = WorkStatusOutbox(tmp_path / "status.sqlite")
    old = WorkStatusProjection(work_id="CR-1-W001", state="RUNNING", revision=1)
    latest = WorkStatusProjection(work_id="CR-1-W001", state="DONE", revision=2)
    outbox.enqueue(old)
    outbox.enqueue(latest)
    with sqlite3.connect(outbox.path) as connection:
        connection.execute(
            """
            UPDATE work_status_outbox
            SET delivery_state='running', lease_token='expired',
                lease_expires_at='2000-01-01T00:00:00Z'
            WHERE idempotency_key=?
            """,
            (old.idempotency_key,),
        )
    transport = RecordingTransport()
    assert outbox.deliver_next(transport) is not None
    assert outbox.deliver_next(transport) is None
    assert [item.state for item in transport.deliveries] == ["DONE"]
    with sqlite3.connect(outbox.path) as connection:
        stale = connection.execute(
            "SELECT superseded_at FROM work_status_outbox WHERE idempotency_key=?",
            (old.idempotency_key,),
        ).fetchone()
    assert stale is not None and stale[0] is not None


def test_failed_old_status_is_superseded_when_a_newer_revision_arrives(tmp_path: Path) -> None:
    outbox = WorkStatusOutbox(tmp_path / "status.sqlite")
    old = WorkStatusProjection(work_id="CR-1-W001", state="RUNNING", revision=1)
    latest = WorkStatusProjection(work_id="CR-1-W001", state="DONE", revision=2)
    outbox.enqueue(old)
    with sqlite3.connect(outbox.path) as connection:
        connection.execute(
            """
            UPDATE work_status_outbox
            SET delivery_state='running', lease_token='failed',
                lease_expires_at='2100-01-01T00:00:00Z'
            WHERE idempotency_key=?
            """,
            (old.idempotency_key,),
        )
    outbox.enqueue(latest)
    outbox._release_lease(old.idempotency_key, "failed")
    with sqlite3.connect(outbox.path) as connection:
        stale = connection.execute(
            "SELECT delivery_state, superseded_at FROM work_status_outbox WHERE idempotency_key=?",
            (old.idempotency_key,),
        ).fetchone()
    assert stale is not None and stale[0] == "delivered" and stale[1] is not None
    assert outbox.pending_count() == 1


def test_project_store_events_queue_an_authoritative_work_snapshot(tmp_path: Path) -> None:
    store = ProjectStore.initialize(str(tmp_path / "project"), "project-a", git_init=False)
    store.register_app("source")
    store.register_app("target")
    change = store.create_change("status projection", "test", "source", affected_apps=["target"])
    store.activate_change(change["change_id"])
    work = store.create_work(
        change["change_id"],
        "target",
        "run verified work",
        source_app="source",
        acceptance=["status is projected"],
    )
    store.activate_work(work["work_id"])
    claimed, token = store.claim_work(work["work_id"], "worker")
    store.start_work(claimed["work_id"], token)

    adapter = WorkStatusProjectionAdapter(store, WorkStatusOutbox(tmp_path / "status.sqlite"))
    assert adapter.sync() == 1
    status = adapter.deliver_next(RecordingTransport())
    assert status is not None
    assert status.work_id == work["work_id"]
    assert status.state == "RUNNING"
