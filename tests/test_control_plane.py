from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from amplai_foundry.control_plane import (
    ApiTokenService,
    ControlPlaneService,
    ControlPlaneStore,
    ControlPlaneWSGIApp,
    JobQueue,
    OutboxDispatcher,
    OutboxQueue,
    ProjectionService,
)
from amplai_foundry.control_plane.connectors import ConnectorRegistry, FileDropConnector
from amplai_foundry.control_plane.errors import AuthenticationError, ConflictError, LeaseError


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
    principal = auth.authenticate(token.raw_token, project_id="project-a", permission="reference:read")
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
    read = service.get_job(job_id=response["job_id"], project_id="project-a", raw_token=token.raw_token)
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


def call_wsgi(app, *, method: str, path: str, token: str, body: dict[str, object] | None = None, idem: str | None = None):
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
