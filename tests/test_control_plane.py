from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest

from amplai_foundry.control_plane import (
    ApiTokenService,
    ContextWorker,
    ControlPlaneService,
    ControlPlaneStore,
    ControlPlaneWSGIApp,
    GovernedKnowledgeIntakeHandler,
    JobQueue,
    OutboxDispatcher,
    OutboxQueue,
    ProjectionService,
)
from amplai_foundry.control_plane.connectors import ConnectorRegistry, FileDropConnector
from amplai_foundry.control_plane.errors import (
    AuthenticationError,
    ConflictError,
    LeaseError,
    NotFoundError,
)
from amplai_foundry.control_plane.http_api import MAX_BODY_BYTES
from amplai_foundry.control_plane.orchestration import (
    OrchestrationBridge,
    OrchestrationBridgeConnector,
    ProjectStoreResolutionHold,
)


@pytest.fixture
def platform(tmp_path: Path):
    store = ControlPlaneStore(tmp_path / "control-plane.db")
    store.initialize()
    auth = ApiTokenService(store)
    token = auth.issue(
        tenant_id="tenant-a",
        project_id="project-a",
        permissions={
            "decision:publish",
            "evidence:publish",
            "context:request",
            "context:read",
            "reference:read",
            "projection:read",
        },
    )
    return store, auth, token, ControlPlaneService(store, auth)


def envelope(kind: str, ref: str = "CR-1-E1") -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "kind": kind,
        "origin": {
            "store": "amplai-project-store",
            "project_id": "project-a",
            "object_ref": ref,
        },
        "origin_content_hash": "sha256:local",
        "idempotency_key": f"kit:{kind}:local",
        "payload": {
            "kind": kind,
            f"{kind}_id": ref,
            "summary" if kind == "evidence" else "statement": "stable result",
        },
    }


def test_token_is_project_scoped_and_raw_value_is_not_persisted(platform) -> None:
    store, auth, token, _service = platform
    principal = auth.authenticate(
        token.raw_token, project_id="project-a", permission="reference:read"
    )
    assert principal.tenant_id == "tenant-a"
    with pytest.raises(AuthenticationError):
        auth.authenticate(token.raw_token, project_id="project-b")
    with store.connect() as connection:
        row = connection.execute("SELECT token_digest FROM cp_api_tokens").fetchone()
    assert row is not None
    assert token.raw_token not in str(row["token_digest"])


def test_publish_is_idempotent_and_origin_conflicts_fail_closed(platform) -> None:
    _store, _auth, token, service = platform
    first = service.publish_reference(
        kind="evidence",
        project_id="project-a",
        raw_token=token.raw_token,
        idempotency_key="idem-1",
        envelope=envelope("evidence"),
    )
    second = service.publish_reference(
        kind="evidence",
        project_id="project-a",
        raw_token=token.raw_token,
        idempotency_key="idem-1",
        envelope=envelope("evidence"),
    )
    assert first == second
    assert first[0] == 201
    changed = envelope("evidence")
    changed["payload"] = {"kind": "evidence", "evidence_id": "CR-1-E1", "summary": "changed"}
    with pytest.raises(ConflictError, match="IDEMPOTENCY"):
        service.publish_reference(
            kind="evidence",
            project_id="project-a",
            raw_token=token.raw_token,
            idempotency_key="idem-1",
            envelope=changed,
        )
    with pytest.raises(ConflictError, match="ORIGIN_REF"):
        service.publish_reference(
            kind="evidence",
            project_id="project-a",
            raw_token=token.raw_token,
            idempotency_key="idem-2",
            envelope=changed,
        )


def test_reference_read_returns_canonical_object(platform) -> None:
    _store, _auth, token, service = platform
    _status, response = service.publish_reference(
        kind="decision",
        project_id="project-a",
        raw_token=token.raw_token,
        idempotency_key="decision-1",
        envelope=envelope("decision", "CR-1-D1"),
    )
    ref = service.get_reference(
        kind="decision",
        canonical_ref=response["canonical_ref"],
        project_id="project-a",
        raw_token=token.raw_token,
    )
    assert ref.origin_ref == "CR-1-D1"
    assert ref.content_digest.startswith("sha256:")


def test_context_job_lease_retry_and_completion(platform) -> None:
    store, _auth, token, service = platform
    _status, response = service.request_context(
        project_id="project-a",
        raw_token=token.raw_token,
        idempotency_key="ctx-1",
        request={"query": "latest active architecture decisions", "max_attempts": 2},
    )
    queue = JobQueue(store)
    first = queue.claim_next(worker_id="worker-1")
    assert first is not None and first.lease_token
    with pytest.raises(LeaseError):
        queue.complete(first.job_id, "wrong", {"items": []})
    retried = queue.fail(first.job_id, first.lease_token, "temporary", retry_delay_seconds=0)
    assert retried.status == "pending"
    second = queue.claim_next(worker_id="worker-1")
    assert second is not None and second.lease_token
    done = queue.complete(second.job_id, second.lease_token, {"items": ["D1"]})
    assert done.status == "done"
    read = service.get_job(
        job_id=response["job_id"], project_id="project-a", raw_token=token.raw_token
    )
    assert read.result == {"items": ["D1"]}


def test_transactional_outbox_delivers_atomic_file(platform, tmp_path: Path) -> None:
    store, _auth, token, service = platform
    service.publish_reference(
        kind="evidence",
        project_id="project-a",
        raw_token=token.raw_token,
        idempotency_key="outbox-1",
        envelope=envelope("evidence", "CR-1-E9"),
    )
    registry = ConnectorRegistry()
    target = tmp_path / "events"
    registry.register("project-events", FileDropConnector(target))
    dispatcher = OutboxDispatcher(OutboxQueue(store), registry)
    delivered = dispatcher.run_once(destination="project-events")
    assert delivered is not None
    assert delivered.status == "delivered"
    paths = list(target.glob("*.json"))
    assert len(paths) == 1
    body = json.loads(paths[0].read_text(encoding="utf-8"))
    assert body["event_type"] == "evidence.published"


def test_projection_can_be_rebuilt_from_events(platform) -> None:
    store, _auth, token, service = platform
    service.publish_reference(
        kind="decision",
        project_id="project-a",
        raw_token=token.raw_token,
        idempotency_key="p-d",
        envelope=envelope("decision", "CR-1-D3"),
    )
    service.publish_reference(
        kind="evidence",
        project_id="project-a",
        raw_token=token.raw_token,
        idempotency_key="p-e",
        envelope=envelope("evidence", "CR-1-E3"),
    )
    service.request_context(
        project_id="project-a",
        raw_token=token.raw_token,
        idempotency_key="p-c",
        request={"query": "context"},
    )
    projection = ProjectionService(store)
    state = projection.rebuild(tenant_id="tenant-a", project_id="project-a")
    assert state["published"] == {"decision": 1, "evidence": 1}
    assert state["jobs"]["requested"] == 1
    assert projection.snapshot(tenant_id="tenant-a", project_id="project-a") == state


def call_wsgi(
    app,
    *,
    method: str,
    path: str,
    token: str,
    body: dict[str, object] | None = None,
    idem: str | None = None,
):
    raw = json.dumps(body or {}).encode("utf-8")
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(raw)),
        "wsgi.input": io.BytesIO(raw),
        "HTTP_AUTHORIZATION": f"Bearer {token}",
    }
    if idem:
        environ["HTTP_IDEMPOTENCY_KEY"] = idem
    captured: dict[str, object] = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = headers

    response = b"".join(app(environ, start_response))
    return captured["status"], json.loads(response)


def test_wsgi_boundary_exposes_authenticated_contract(platform) -> None:
    _store, _auth, token, service = platform
    app = ControlPlaneWSGIApp(service)
    status, body = call_wsgi(
        app,
        method="POST",
        path="/v1/projects/project-a/evidence",
        token=token.raw_token,
        body=envelope("evidence", "CR-1-E20"),
        idem="http-1",
    )
    assert status == "201 Created"
    assert body["canonical_ref"].startswith("evidence:project-a:")
    status, projection = call_wsgi(
        app,
        method="GET",
        path="/v1/projects/project-a/projection",
        token=token.raw_token,
    )
    assert status == "200 OK"
    assert projection["published"]["evidence"] == 1


def identical_payload_envelope(ref: str) -> dict[str, object]:
    """Same content from a different origin.

    `canonical_ref` must not be a function of the payload alone — two origins can
    legitimately carry byte-identical content.
    """
    item = envelope("decision", ref)
    item["payload"] = {"kind": "decision", "statement": "byte-identical content"}
    return item


def test_same_payload_from_two_origins_gets_distinct_canonical_refs(platform) -> None:
    _store, _auth, token, service = platform
    _status, first = service.publish_reference(
        kind="decision",
        project_id="project-a",
        raw_token=token.raw_token,
        idempotency_key="origin-1",
        envelope=identical_payload_envelope("CR-9-D1"),
    )
    _status, second = service.publish_reference(
        kind="decision",
        project_id="project-a",
        raw_token=token.raw_token,
        idempotency_key="origin-2",
        envelope=identical_payload_envelope("CR-9-D2"),
    )
    assert first["canonical_ref"] != second["canonical_ref"]
    assert first["content_digest"] == second["content_digest"]
    assert first["duplicate_origin"] is False
    assert second["duplicate_origin"] is False
    for response, expected in ((first, "CR-9-D1"), (second, "CR-9-D2")):
        reference = service.get_reference(
            kind="decision",
            canonical_ref=str(response["canonical_ref"]),
            project_id="project-a",
            raw_token=token.raw_token,
        )
        assert reference.origin_ref == expected


def test_two_tenants_sharing_a_project_id_do_not_collide(platform) -> None:
    _store, auth, token_a, service = platform
    token_b = auth.issue(
        tenant_id="tenant-b",
        project_id="project-a",
        permissions={"decision:publish", "reference:read"},
    )
    _status, owned_by_a = service.publish_reference(
        kind="decision",
        project_id="project-a",
        raw_token=token_a.raw_token,
        idempotency_key="shared-1",
        envelope=identical_payload_envelope("CR-9-SHARED"),
    )
    _status, owned_by_b = service.publish_reference(
        kind="decision",
        project_id="project-a",
        raw_token=token_b.raw_token,
        idempotency_key="shared-1",
        envelope=identical_payload_envelope("CR-9-SHARED"),
    )
    assert owned_by_a["canonical_ref"] != owned_by_b["canonical_ref"]
    with pytest.raises(NotFoundError):
        service.get_reference(
            kind="decision",
            canonical_ref=str(owned_by_a["canonical_ref"]),
            project_id="project-a",
            raw_token=token_b.raw_token,
        )


def raw_wsgi(app, *, environ: dict[str, object]):
    """Drive the app with a hand-built environ and report whether it kept the contract.

    A WSGI application must call `start_response` and return an iterable body for
    every outcome, including failures.  These tests assert that, so the helper
    reports the escape rather than swallowing it.
    """
    captured: dict[str, object] = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = headers

    body = b"".join(app(environ, start_response))
    return captured.get("status"), json.loads(body)


def authed_environ(token: str, *, body: bytes, path: str, idem: str = "guard-1"):
    return {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
        "HTTP_AUTHORIZATION": f"Bearer {token}",
        "HTTP_IDEMPOTENCY_KEY": idem,
    }


@pytest.mark.parametrize(
    "limit",
    [[1], {"a": 1}, True, 0, 501],
    ids=["list", "object", "bool", "zero", "above-max"],
)
def test_context_request_rejects_a_limit_that_is_not_a_bounded_int(platform, limit) -> None:
    _store, _auth, token, service = platform
    app = ControlPlaneWSGIApp(service)
    raw = json.dumps({"query": "q", "limit": limit}).encode("utf-8")
    status, body = raw_wsgi(
        app,
        environ=authed_environ(
            token.raw_token,
            body=raw,
            path="/v1/projects/project-a/context-requests",
            idem=f"limit-{limit!r}",
        ),
    )
    assert status is not None and status.startswith("400"), status
    assert "LIMIT" in body["error"]


@pytest.mark.parametrize("attempts", [0, 11, "many"], ids=["zero", "above-max", "text"])
def test_context_request_rejects_an_out_of_range_max_attempts(platform, attempts) -> None:
    _store, _auth, token, service = platform
    app = ControlPlaneWSGIApp(service)
    raw = json.dumps({"query": "q", "max_attempts": attempts}).encode("utf-8")
    status, body = raw_wsgi(
        app,
        environ=authed_environ(
            token.raw_token,
            body=raw,
            path="/v1/projects/project-a/context-requests",
            idem=f"attempts-{attempts!r}",
        ),
    )
    assert status is not None and status.startswith("400"), status
    assert "MAX_ATTEMPTS" in body["error"]


def test_context_request_accepts_a_numeric_string_limit(platform) -> None:
    _store, _auth, token, service = platform
    _status, response = service.request_context(
        project_id="project-a",
        raw_token=token.raw_token,
        idempotency_key="limit-string",
        request={"query": "q", "limit": "25"},
    )
    job = service.get_job(
        job_id=response["job_id"], project_id="project-a", raw_token=token.raw_token
    )
    assert job.payload["limit"] == 25


class RefusingStream:
    """A body stream that fails the test if the boundary reads it."""

    def read(self, size: int = -1) -> bytes:  # pragma: no cover - must not run
        raise AssertionError("the oversized body must be rejected before it is read")


def test_oversized_declared_body_is_rejected_without_reading_it(platform) -> None:
    _store, _auth, token, service = platform
    app = ControlPlaneWSGIApp(service)
    status, body = raw_wsgi(
        app,
        environ={
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/v1/projects/project-a/decision",
            "CONTENT_LENGTH": str(MAX_BODY_BYTES + 1),
            "wsgi.input": RefusingStream(),
            "HTTP_AUTHORIZATION": f"Bearer {token.raw_token}",
            "HTTP_IDEMPOTENCY_KEY": "oversized-1",
        },
    )
    assert status is not None and status.startswith("413"), status
    assert body["error"] == "PAYLOAD_TOO_LARGE"


@pytest.mark.parametrize("length", ["-1", "abc"], ids=["negative", "not-an-int"])
def test_unusable_content_length_is_rejected(platform, length) -> None:
    _store, _auth, token, service = platform
    app = ControlPlaneWSGIApp(service)
    status, body = raw_wsgi(
        app,
        environ={
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/v1/projects/project-a/decision",
            "CONTENT_LENGTH": length,
            "wsgi.input": RefusingStream(),
            "HTTP_AUTHORIZATION": f"Bearer {token.raw_token}",
            "HTTP_IDEMPOTENCY_KEY": f"length-{length}",
        },
    )
    assert status is not None and status.startswith("400"), status
    assert body["error"] == "CONTENT_LENGTH_INVALID"


class ExplodingService:
    """Stands in for any unexpected failure below the boundary."""

    def __init__(self, service, error: BaseException) -> None:
        self._service = service
        self._error = error
        self.store = service.store
        self.auth = service.auth

    def publish_reference(self, **_kwargs):
        raise self._error


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (sqlite3.IntegrityError("UNIQUE constraint failed: cp_objects.canonical_ref"), "409"),
        (sqlite3.OperationalError("database is locked"), "503"),
        (TypeError("int() argument must be a string"), "500"),
        (RuntimeError("a leaked internal detail"), "500"),
    ],
    ids=["integrity", "operational", "type", "unexpected"],
)
def test_every_failure_still_produces_a_wsgi_response(platform, error, expected_status) -> None:
    _store, _auth, token, service = platform
    app = ControlPlaneWSGIApp(ExplodingService(service, error))
    raw = json.dumps(envelope("decision", "CR-9-BOOM")).encode("utf-8")
    status, body = raw_wsgi(
        app,
        environ=authed_environ(
            token.raw_token, body=raw, path="/v1/projects/project-a/decision", idem="boom-1"
        ),
    )
    assert status is not None and status.startswith(expected_status), status
    assert "error" in body
    assert str(error) not in json.dumps(body), "the boundary must not echo internal detail"


def orchestration_request(*, target_app_hint: str | None = "amplai-foundry") -> dict[str, object]:
    return {
        "schema_version": 1,
        "controller": "work",
        "goal": "Add a narrow verified integration",
        "project_hint": "project-a",
        "target_app_hint": target_app_hint,
        "runner_hint": "codex",
        "artifact_refs": ["ART-001"],
        "reply_route": {
            "provider": "slack",
            "workspace_id": "T001",
            "channel_id": "C001",
            "thread_id": "1710000000.000001",
        },
    }


def test_orchestration_request_is_durable_idempotent_and_emits_outbox(platform) -> None:
    store, auth, _token, service = platform
    service_token = auth.issue(
        tenant_id="tenant-a",
        project_id="project-a",
        permissions={"orchestration.request.submit", "orchestration.request.read"},
    )
    request = orchestration_request()
    first = service.submit_orchestration_request(
        project_id="project-a",
        raw_token=service_token.raw_token,
        idempotency_key="slack:T001:C001:abc",
        request=request,
    )
    second = service.submit_orchestration_request(
        project_id="project-a",
        raw_token=service_token.raw_token,
        idempotency_key="slack:T001:C001:abc",
        request=request,
    )
    assert first == second
    assert first[0] == 202
    assert first[1]["status"] == "REQUESTED"
    with store.connect() as connection:
        row = connection.execute(
            "SELECT event_type, aggregate_ref FROM cp_outbox WHERE aggregate_ref=?",
            (first[1]["request_id"],),
        ).fetchone()
    assert row is not None
    assert row["event_type"] == "orchestration.requested"


def test_orchestration_request_holds_unknown_target_without_runnable_work(platform) -> None:
    _store, auth, _token, service = platform
    service_token = auth.issue(
        tenant_id="tenant-a",
        project_id="project-a",
        permissions={"orchestration.request.submit", "orchestration.request.read"},
    )
    status, response = service.submit_orchestration_request(
        project_id="project-a",
        raw_token=service_token.raw_token,
        idempotency_key="slack:T001:C001:hold",
        request=orchestration_request(target_app_hint=None),
    )
    assert status == 202
    assert response["status"] == "RESOLUTION_HOLD"
    assert response["hold_code"] == "TARGET_APP_REQUIRED"


def test_orchestration_wsgi_and_read_contract(platform) -> None:
    _store, auth, _token, service = platform
    token = auth.issue(
        tenant_id="tenant-a",
        project_id="project-a",
        permissions={"orchestration.request.submit", "orchestration.request.read"},
    )
    app = ControlPlaneWSGIApp(service)
    status, body = call_wsgi(
        app,
        method="POST",
        path="/v1/projects/project-a/orchestration-requests",
        token=token.raw_token,
        body=orchestration_request(),
        idem="slack:T001:C001:http",
    )
    assert status == "202 Accepted"
    status, fetched = call_wsgi(
        app,
        method="GET",
        path=f"/v1/projects/project-a/orchestration-requests/{body['request_id']}",
        token=token.raw_token,
    )
    assert status == "200 OK"
    assert fetched["request_id"] == body["request_id"]
    assert fetched["goal"] == "Add a narrow verified integration"


def test_status_controller_is_persisted_in_fresh_control_plane_schema(platform) -> None:
    _store, auth, _token, service = platform
    token = auth.issue(
        tenant_id="tenant-a",
        project_id="project-a",
        permissions={"orchestration.request.submit"},
    )
    request = orchestration_request()
    request["controller"] = "status"
    status, response = service.submit_orchestration_request(
        project_id="project-a",
        raw_token=token.raw_token,
        idempotency_key="slack:T001:C001:status",
        request=request,
    )
    assert status == 202
    assert response["status"] == "RESOLUTION_HOLD"
    assert response["hold_code"] == "STATUS_QUERY_REQUIRES_WORK_REF"


def test_knowledge_intake_creates_a_dedicated_governed_job(platform) -> None:
    _store, auth, _token, service = platform
    token = auth.issue(
        tenant_id="tenant-a",
        project_id="project-a",
        permissions={"orchestration.request.submit", "context:read"},
    )
    status, response = service.request_knowledge_intake(
        project_id="project-a",
        raw_token=token.raw_token,
        idempotency_key="slack:T001:C001:intake",
        request={
            "source_text": "Keep this source verbatim for governed curation.",
            "reply_route": {"provider": "slack", "workspace_id": "T001", "channel_id": "C001"},
        },
    )
    assert status == 202
    job = service.get_job(
        job_id=response["job_id"], project_id="project-a", raw_token=token.raw_token
    )
    assert job.kind == "knowledge.intake"
    assert job.payload["workflow"] == "source-preserve_then_governed_curation"


def test_knowledge_intake_worker_stops_at_source_and_curation_context(
    platform, tmp_path: Path
) -> None:
    store, auth, _token, service = platform
    token = auth.issue(
        tenant_id="tenant-a",
        project_id="project-a",
        permissions={"orchestration.request.submit", "context:read"},
    )
    status, _response = service.request_knowledge_intake(
        project_id="project-a",
        raw_token=token.raw_token,
        idempotency_key="slack:T001:C001:intake-worker",
        request={
            "source_text": "Preserve these exact words before curation.",
            "reply_route": {"provider": "slack", "workspace_id": "T001", "channel_id": "C001"},
        },
    )
    assert status == 202
    vault = tmp_path / "vault"
    (vault / "projects" / "amplai").mkdir(parents=True)
    handler = GovernedKnowledgeIntakeHandler(
        vault=vault,
        knowledge_project="amplai",
        workspace=tmp_path,
        repository_root=tmp_path,
    )
    completed = ContextWorker(JobQueue(store), handler).run_once(kind="knowledge.intake")
    assert completed is not None and completed.status == "done"
    assert completed.result is not None
    assert completed.result["canonical_mutation"] == "not_attempted"
    assert Path(str(completed.result["source_path"])).is_file()
    assert Path(str(completed.result["curation_context"])).is_file()
    proposal_path = Path(str(completed.result["proposal_path"]))
    assert proposal_path.is_file()
    assert completed.result["proposal_status"] == "draft"
    assert "IGNORE" in proposal_path.read_text(encoding="utf-8")


def test_kind_routed_knowledge_worker_leaves_other_jobs_pending(platform, tmp_path: Path) -> None:
    store, auth, _token, service = platform
    token = auth.issue(
        tenant_id="tenant-a",
        project_id="project-a",
        permissions={"context:request", "orchestration.request.submit", "context:read"},
    )
    service.request_context(
        project_id="project-a",
        raw_token=token.raw_token,
        idempotency_key="context-first",
        request={"query": "do not claim me"},
    )
    service.request_knowledge_intake(
        project_id="project-a",
        raw_token=token.raw_token,
        idempotency_key="knowledge-second",
        request={
            "source_text": "Only this job is eligible.",
            "reply_route": {"provider": "slack", "workspace_id": "T001", "channel_id": "C001"},
        },
    )
    vault = tmp_path / "vault"
    (vault / "projects" / "amplai").mkdir(parents=True)
    worker = ContextWorker(
        JobQueue(store),
        GovernedKnowledgeIntakeHandler(
            vault=vault,
            knowledge_project="amplai",
            workspace=tmp_path,
            repository_root=tmp_path,
        ),
    )
    assert worker.run_once(kind="knowledge.intake") is not None
    with store.connect() as connection:
        row = connection.execute("SELECT status FROM cp_jobs WHERE kind='context.build'").fetchone()
    assert row is not None and row["status"] == "pending"


class FakeProjectStoreGateway:
    def __init__(self) -> None:
        self.work_by_request: dict[str, str] = {}

    def ensure_draft_work(self, request) -> str:
        return self.work_by_request.setdefault(request.request_id, "CR-1-W001")


class HoldingProjectStoreGateway:
    def ensure_draft_work(self, request) -> str:
        raise ProjectStoreResolutionHold("TARGET_APP_NOT_REGISTERED")


class MismatchedProjectStoreGateway:
    def ensure_draft_work(self, request) -> str:
        raise ProjectStoreResolutionHold("PROJECT_STORE_PROJECT_MISMATCH")


def test_bridge_retry_creates_one_draft_work_for_one_request(platform) -> None:
    store, auth, _token, service = platform
    token = auth.issue(
        tenant_id="tenant-a",
        project_id="project-a",
        permissions={"orchestration.request.submit", "orchestration.request.read"},
    )
    _status, response = service.submit_orchestration_request(
        project_id="project-a",
        raw_token=token.raw_token,
        idempotency_key="slack:T001:C001:bridge",
        request=orchestration_request(),
    )
    gateway = FakeProjectStoreGateway()
    bridge = OrchestrationBridge(store, gateway)
    first = bridge.bridge_request(
        tenant_id="tenant-a", project_id="project-a", request_id=response["request_id"]
    )
    second = bridge.bridge_request(
        tenant_id="tenant-a", project_id="project-a", request_id=response["request_id"]
    )
    assert first.work_ref == second.work_ref == "CR-1-W001"
    assert gateway.work_by_request == {response["request_id"]: "CR-1-W001"}


def test_bridge_maps_a_project_store_mismatch_to_a_durable_hold(platform) -> None:
    store, auth, _token, service = platform
    token = auth.issue(
        tenant_id="tenant-a",
        project_id="project-a",
        permissions={"orchestration.request.submit", "orchestration.request.read"},
    )
    _status, response = service.submit_orchestration_request(
        project_id="project-a",
        raw_token=token.raw_token,
        idempotency_key="slack:T001:C001:project-mismatch",
        request=orchestration_request(),
    )

    held = OrchestrationBridge(store, MismatchedProjectStoreGateway()).bridge_request(
        tenant_id="tenant-a", project_id="project-a", request_id=response["request_id"]
    )
    assert held.status == "RESOLUTION_HOLD"
    assert held.hold_code == "PROJECT_STORE_PROJECT_MISMATCH"


def test_outbox_worker_composes_request_to_draft_work(platform) -> None:
    store, auth, _token, service = platform
    token = auth.issue(
        tenant_id="tenant-a",
        project_id="project-a",
        permissions={"orchestration.request.submit", "orchestration.request.read"},
    )
    _status, response = service.submit_orchestration_request(
        project_id="project-a",
        raw_token=token.raw_token,
        idempotency_key="slack:T001:C001:outbox-bridge",
        request=orchestration_request(),
    )
    connectors = ConnectorRegistry()
    connectors.register(
        OrchestrationBridgeConnector.destination,
        OrchestrationBridgeConnector(OrchestrationBridge(store, FakeProjectStoreGateway())),
    )
    delivered = OutboxDispatcher(OutboxQueue(store), connectors).run_once(
        destination=OrchestrationBridgeConnector.destination
    )
    assert delivered is not None and delivered.status == "delivered"
    request = service.get_orchestration_request(
        request_id=response["request_id"], project_id="project-a", raw_token=token.raw_token
    )
    assert request.status == "BRIDGED" and request.work_ref == "CR-1-W001"


def test_bridge_queues_an_activation_card_after_creating_draft_work(platform) -> None:
    store, auth, _token, service = platform
    token = auth.issue(
        tenant_id="tenant-a",
        project_id="project-a",
        permissions={"orchestration.request.submit", "orchestration.request.read"},
    )
    _status, response = service.submit_orchestration_request(
        project_id="project-a",
        raw_token=token.raw_token,
        idempotency_key="slack:T001:C001:activation-card",
        request=orchestration_request(),
    )

    class Scheduler:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def schedule(self, *, request, work_ref: str) -> str:
            self.calls.append((request.request_id, work_ref))
            return "activation-card:fixture"

    scheduler = Scheduler()
    connectors = ConnectorRegistry()
    connectors.register(
        OrchestrationBridgeConnector.destination,
        OrchestrationBridgeConnector(
            OrchestrationBridge(store, FakeProjectStoreGateway()), card_scheduler=scheduler
        ),
    )
    assert (
        OutboxDispatcher(OutboxQueue(store), connectors).run_once(
            destination=OrchestrationBridgeConnector.destination
        )
        is not None
    )
    assert scheduler.calls == [(response["request_id"], "CR-1-W001")]


def test_orchestration_work_endpoint_reads_authoritative_work_not_request_row(platform) -> None:
    store, auth, _token, service = platform
    token = auth.issue(
        tenant_id="tenant-a",
        project_id="project-a",
        permissions={
            "orchestration.request.submit",
            "orchestration.request.read",
            "orchestration.work.read",
        },
    )
    _status, response = service.submit_orchestration_request(
        project_id="project-a",
        raw_token=token.raw_token,
        idempotency_key="slack:T001:C001:work-read",
        request=orchestration_request(),
    )
    OrchestrationBridge(store, FakeProjectStoreGateway()).bridge_request(
        tenant_id="tenant-a", project_id="project-a", request_id=response["request_id"]
    )

    class Reader:
        def get_work(self, work_id: str):
            assert work_id == "CR-1-W001"
            return {"work_id": work_id, "status": "RUNNING", "attempt": 2}

    read_service = ControlPlaneService(store, auth, work_reader=Reader())
    assert read_service.get_orchestration_work(
        work_id="CR-1-W001", project_id="project-a", raw_token=token.raw_token
    ) == {"work_id": "CR-1-W001", "status": "RUNNING", "attempt": 2}


def test_bridge_holds_unregistered_target_without_creating_work(platform) -> None:
    store, auth, _token, service = platform
    token = auth.issue(
        tenant_id="tenant-a",
        project_id="project-a",
        permissions={"orchestration.request.submit", "orchestration.request.read"},
    )
    _status, response = service.submit_orchestration_request(
        project_id="project-a",
        raw_token=token.raw_token,
        idempotency_key="slack:T001:C001:unregistered",
        request=orchestration_request(),
    )
    held = OrchestrationBridge(store, HoldingProjectStoreGateway()).bridge_request(
        tenant_id="tenant-a", project_id="project-a", request_id=response["request_id"]
    )
    assert held.status == "RESOLUTION_HOLD"
    assert held.hold_code == "TARGET_APP_NOT_REGISTERED"
    assert held.work_ref is None
