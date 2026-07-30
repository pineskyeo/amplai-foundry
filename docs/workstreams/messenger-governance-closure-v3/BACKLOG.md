# Messenger Governance Closure V3 Backlog

## Items

### MGC-001 — Governance Store Foundation

```yaml
id: MGC-001
type: backend-feature
project: amplai-foundry
target: governance persistence foundation
title: Governance Store Foundation
status: implementation-ready
priority: P0
depends_on: []
pipeline: design-reviewed → implement → test → subagent-review → gate
forbidden:
  - application behavior migration
  - Provider adapter implementation
risk_level: high
owner_agent: main-codex
```

SQLite schema migration, connection policy, transaction helper와 runtime integrity check를 추가한다.

### MGC-002 — Immutable Definition Store

```yaml
id: MGC-002
type: backend-feature
project: amplai-foundry
target: Proposal definition storage
title: Immutable Definition Store
status: planned
priority: P0
depends_on: [MGC-001]
pipeline: implement → test → subagent-review → gate
forbidden:
  - mutable definition overwrite
  - local-id-only API
risk_level: high
owner_agent: main-codex
```

Content-addressed definition/input object와 digest 검증을 구현한다.

### MGC-003 — Active Definition CAS And State Machine

```yaml
id: MGC-003
type: backend-feature
project: amplai-foundry
target: active definition pointer and Proposal state
title: Active Definition CAS And State Machine
status: planned
priority: P0
depends_on: [MGC-002]
pipeline: implement → test → subagent-review → gate
forbidden:
  - filesystem I/O inside SQLite transaction
risk_level: high
owner_agent: main-codex
```

Qualified active pointer CAS, revisions, decision epoch와 state transition을 구현한다.

### MGC-004 — ActionToken And Decision Replay

```yaml
id: MGC-004
type: backend-feature
project: amplai-foundry
target: governed Proposal decisions
title: ActionToken And Decision Replay
status: planned
priority: P0
depends_on: [MGC-003]
pipeline: implement → failure-test → subagent-review → gate
forbidden:
  - raw Token persistence
  - replay-after-stale validation
risk_level: critical
owner_agent: main-codex
```

Hash-only Token, replay precedence와 atomic decision transaction을 구현한다.

### MGC-005 — Durable Provider Ingress

```yaml
id: MGC-005
type: backend-feature
project: amplai-foundry
target: Provider ingress queue
title: Durable Provider Ingress
status: planned
priority: P0
depends_on: [MGC-004]
pipeline: implement → hard-kill-test → subagent-review → gate
forbidden:
  - ack before ingress commit
  - raw request body persistence
risk_level: critical
owner_agent: main-codex
```

Ingress lease, reclaim, retry, DLQ와 commit-before-ack 계약을 구현한다.

### MGC-006 — Authority And Actor Binding

```yaml
id: MGC-006
type: backend-feature
project: amplai-foundry
target: AuthorityService and binding governance
title: Authority And Actor Binding
status: planned
priority: P0
depends_on: [MGC-003]
pipeline: implement → security-test → subagent-review → gate
forbidden:
  - client-created AuthorityContext
  - silent binding overwrite
risk_level: high
owner_agent: main-codex
```

Server-only Authority와 governed Actor binding transition을 구현한다.

### MGC-007 — Direct Mutation Closure

```yaml
id: MGC-007
type: refactor
project: amplai-foundry
target: CLI and Intake mutation paths
title: Direct Mutation Closure
status: planned
priority: P0
depends_on: [MGC-004, MGC-006]
pipeline: inventory → refactor → architecture-test → subagent-review → gate
forbidden:
  - direct approve_proposal production call
  - direct ProposalApplyService public call
risk_level: high
owner_agent: main-codex
```

CLI와 Intake mutation을 Governance Service로 라우팅한다.

### MGC-008 — Ordered Transactional Outbox

```yaml
id: MGC-008
type: backend-feature
project: amplai-foundry
target: YAML and Provider projections
title: Ordered Transactional Outbox
status: planned
priority: P0
depends_on: [MGC-004, MGC-005]
pipeline: implement → ordering-test → subagent-review → gate
forbidden:
  - unordered destination delivery
risk_level: high
owner_agent: main-codex
```

Aggregate sequence, destination fencing, retry, supersede와 DLQ를 구현한다.

### MGC-009 — ApplyGrant And Apply Job

```yaml
id: MGC-009
type: backend-feature
project: amplai-foundry
target: apply request authority
title: ApplyGrant And Apply Job
status: planned
priority: P0
depends_on: [MGC-004, MGC-006]
pipeline: implement → concurrency-test → subagent-review → gate
forbidden:
  - ActionToken apply use
risk_level: critical
owner_agent: main-codex
```

Approved snapshot scoped Grant와 atomic ApplyJob 생성을 구현한다.

### MGC-010 — Fenced Publish Coordinator

```yaml
id: MGC-010
type: backend-feature
project: amplai-foundry
target: canonical apply publication
title: Fenced Publish Coordinator
status: planned
priority: P0
depends_on: [MGC-008, MGC-009]
pipeline: stage → publish-intent → Git-CAS → recovery-test → subagent-review → gate
forbidden:
  - Worker canonical write
  - timed reclaim of prepared publish
risk_level: critical
owner_agent: main-codex
```

Isolated staging, durable publish intent, Git ref CAS와 recovery를 구현한다.

### MGC-011 — V2 Migration

```yaml
id: MGC-011
type: backend-feature
project: amplai-foundry
target: Proposal governance migration
title: V2 Migration
status: done
priority: P1
depends_on: [MGC-003, MGC-004, MGC-009]
pipeline: dry-run → import → verify → rollback-test → subagent-review → gate
forbidden:
  - destructive legacy overwrite
risk_level: high
owner_agent: main-codex
```

Legacy Proposal과 audit를 v3 store로 import한다.

### MGC-012 — Slack Reference Adapter

```yaml
id: MGC-012
type: backend-feature
project: amplai-foundry
target: Slack adapter
title: Slack Reference Adapter
status: implementation-ready
priority: P1
depends_on: [MGC-005, MGC-008]
pipeline: implement → sandbox-E2E → subagent-review → gate
forbidden:
  - interaction_payload_id dependency
risk_level: high
owner_agent: main-codex
```

Raw signature verification, durable ack path와 message outbox를 구현한다.

### MGC-013 — Telegram Reference Adapter

```yaml
id: MGC-013
type: backend-feature
project: amplai-foundry
target: Telegram adapter
title: Telegram Reference Adapter
status: planned
priority: P1
depends_on: [MGC-005, MGC-008]
pipeline: implement → sandbox-E2E → subagent-review → gate
forbidden:
  - callback data over 64 bytes
risk_level: high
owner_agent: main-codex
```

Webhook secret, `update_id` deduplication과 independent E2E를 구현한다.

### MGC-014 — Hermes Skill

```yaml
id: MGC-014
type: backend-feature
project: amplai-foundry
target: Hermes capability adapter
title: Hermes Skill
status: planned
priority: P1
depends_on: [MGC-006, MGC-008, MGC-009]
pipeline: contract → implement → least-privilege-test → subagent-review → gate
forbidden:
  - shell capability
  - permission construction
risk_level: medium
owner_agent: main-codex
```

Hermes 최소 권한 Skill contract를 구현한다.

### MGC-015 — Activation Control

```yaml
id: MGC-015
type: backend-feature
project: amplai-foundry
target: Project-Provider-Feature activation
title: Activation Control
status: planned
priority: P1
depends_on: [MGC-010, MGC-012, MGC-013]
pipeline: implement → failure-policy-test → subagent-review → gate
forbidden:
  - global Provider enable
risk_level: high
owner_agent: main-codex
```

Evidence-bound Activation Gate와 runtime fail-closed policy를 구현한다.

### MGC-016 — Full Verification And Closure

```yaml
id: MGC-016
type: runbook
project: amplai-foundry
target: repository completion gate
title: Full Verification And Closure
status: planned
priority: P1
depends_on: [MGC-011, MGC-012, MGC-013, MGC-014, MGC-015]
pipeline: clean-clone → verify → E2E → subagent-review → close
forbidden:
  - unexecuted verification reported as passed
risk_level: high
owner_agent: main-codex
```

Clean-clone, failure injection, Provider E2E와 final review를 실행한다.
