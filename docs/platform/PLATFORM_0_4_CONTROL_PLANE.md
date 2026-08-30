# AMPLAI Platform 0.4 Control Plane

Status: implementation candidate.  This release adds the service/control-plane boundary without rewriting the existing Governance Store or the Loop Kit Project Store.

## Boundary

- Platform owns canonical Source/Knowledge/Decision references, project-scoped authentication, durable service jobs, event/outbox delivery, and replayable projections.
- Loop Kit owns repository-local Work/Attempt/Lease/Session and local Decision/Evidence until explicit promotion.
- Claude Code and Codex are host adapters of the Kit, never Platform domain dependencies.

Dependency direction:

```text
Claude Code / Codex
        ↓
AMPLAI Loop Kit 2.4
        ↓ promotion / context request
AMPLAI Platform 0.4 Control Plane
        ↓
existing Foundry Governance + Vault
```

## Platform 0.4 APIs

The dependency-free WSGI boundary exposes:

- `POST /v1/projects/{project}/evidence`
- `POST /v1/projects/{project}/decision`
- `POST /v1/projects/{project}/context-requests`
- `GET /v1/projects/{project}/references/{kind}/{canonical_ref}`
- `GET /v1/projects/{project}/jobs/{job_id}`
- `GET /v1/projects/{project}/projection`

Writes require both `Authorization: Bearer ...` and `Idempotency-Key`.
Tokens are project-scoped and only their SHA-256 digest is persisted.

## Durable execution

Context work uses `pending → running → done/dead` with lease token, expiry, heartbeat/retry semantics.  Event creation and outbox insertion occur in the same SQLite transaction.  Delivery uses a separate lease so service transactions never perform external I/O.

## Connector isolation

Connector configuration identifies a destination and optional secret reference.  Raw secret material is resolved at send time by a `SecretResolver` and is not stored in the Control Plane DB or outbox payload.  `FileDropConnector` is included as an offline deterministic destination and can HMAC-sign event files.

## Projection recovery

`ProjectionService.rebuild()` replays the append-only `cp_events` sequence and atomically replaces the project summary projection.  Projection state is disposable; events and canonical objects are authoritative.

## Local run

```bash
amplai-foundry control-plane init --db .amplai/control-plane.db
amplai-foundry control-plane token-issue \
  --db .amplai/control-plane.db \
  --tenant local --project ai-platform \
  --permission evidence:publish \
  --permission decision:publish \
  --permission context:request \
  --permission context:read \
  --permission reference:read \
  --permission projection:read
amplai-foundry control-plane serve --db .amplai/control-plane.db
```

## Release gate still requiring a real host

The deterministic implementation tests do not substitute for an authenticated real-host run.  Before unattended use, run one fresh Work and one resumed Work on both Claude Code and Codex, review Codex `/hooks`, then keep `auto_start=false` until those four runs are captured as evidence.
