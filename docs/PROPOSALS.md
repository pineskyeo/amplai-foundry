# Proposals

## Domain Contract

Proposal은 Source evidence에서 파생한 canonical knowledge 변경 제안이다. 상태는 `draft`, `reviewed`, `changes_requested`, `approved`, `applied`, `rejected`, `superseded`다.

Operation은 `CREATE`, `UPDATE`, `LINK`, `MERGE`, `SPLIT`, `SUPERSEDE`, `CONFLICT`, `IGNORE`를 지원한다. Confidence는 사실 여부가 아니라 curator 판단 강도이며 `low`, `medium`, `high`만 사용한다.

Proposal artifact는 `.amplai/proposals/{proposal_id}/`에 저장한다. `proposal.yaml`, `summary.md`, `drafts/`, `patches/`를 Git에 포함한다.

## Validation

```bash
amplai-foundry proposal validate .amplai/proposals/PROP-.../proposal.yaml
amplai-foundry proposal show PROP-...
amplai-foundry proposal diff PROP-...
```

Validation은 Pydantic schema, Source, evidence Source, target, draft scope를 확인한다. Draft는 Proposal의 `drafts/` directory 밖을 참조할 수 없다.

Source는 evidence로 참조할 수 있지만 Proposal로 생성하거나 변경할 수 없다. `CREATE kind=source`와 Source 대상 `UPDATE`, `LINK`, `SUPERSEDE`, `MERGE`, `SPLIT`은 거부한다.

Proposal의 `source_ids`, evidence Source, target, draft는 모두 `Proposal.project`와 같은 project에 속한다. Draft namespace는 `Proposal.namespace`와 일치한다. CREATE destination은 draft가 아니라 검증된 Proposal project root에서 계산한다.

## Optimistic Concurrency

새 `draft`, `reviewed`, `approved` Proposal의 `UPDATE`, `LINK`, `SUPERSEDE` operation은 다음 precondition을 기록한다.

```yaml
expected_revision: 3
expected_target_sha256: 64자리-lowercase-sha256
```

`expected_target_sha256`은 Proposal 생성 시점 canonical target Markdown raw bytes의 SHA-256이다. Apply lock 안에서 현재 revision과 hash를 다시 검사한다. 하나라도 다르면 `PROPOSAL_STALE_REVISION` 또는 `PROPOSAL_STALE_TARGET_HASH`로 전체 Proposal을 거부한다. Vault와 Proposal status는 변경하지 않는다.

이미 `applied`인 v1 Proposal은 역사적 artifact다. Precondition이 없어도 계속 load와 validation이 가능하며 기존 artifact를 다시 쓰지 않는다.

## Decision And Apply Boundary

CLI direct decision과 apply는 미사용이다.

`approve`, `reject`, `request_changes`는 SQLite Governance Store의
`DecisionService`만 수행한다. 이 service는 live Actor binding, Project permission,
`ActionToken`, revision과 digest를 같은 transaction에서 다시 확인한다.

Direct apply는 `APPLY_ACTION_DEFERRED`를 반환한다. `MGC-009`가 `ApplyGrant`와
Apply Job을 소유한다. Legacy apply engine은 migration regression fixture용 private
code이며 production entrypoint가 아니다.

Git publish 계약은 `MGC-010`이 소유한다.

## Audit And Projection

Accepted Decision은 Proposal state, ActionToken consumption, idempotency result,
hash-chained Audit와 Outbox event를 하나의 SQLite transaction에 기록한다. Audit row는
update/delete할 수 없다. Outbox payload도 immutable이다.

Projection은 authority가 아니다. YAML과 Provider message는 destination sequence대로
Outbox에서 전달한다. Dispatcher는 lease와 fencing generation을 사용한다. Retry 한도 초과나
remote reconcile 불가는 DLQ와 operator hold를 만든다.

YAML projection은 `source_state_revision`과 `aggregate_sequence` CAS를 통과해야 한다.
SQLite authoritative state보다 앞서거나 reverse-order인 projection write는 거부한다.

## Messenger Decision Action

Slack, Telegram, Hermes와 다른 UI는 [Messenger Proposal Control](MESSENGER-PROPOSAL-CONTROL.md)의 channel-independent `ProposalAction`을 사용한다.

Message button은 authority가 아니다. AMPLAI가 qualified Proposal identity, server-created authority, revision, digest, token, idempotency와 현재 상태를 다시 검사한다.
