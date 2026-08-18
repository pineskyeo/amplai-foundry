# Research: Slack Proposal Cards

## Scope

이 문서는 Slack Proposal Card 구현에 필요한 외부 contract와 기존 repository 경계를 확정한다.
제품 요구사항은 `spec.md`가 소유한다.

## External Facts

### R-001 Card Block

Slack Block Kit은 `type: card` block을 제공한다. Card는 `title`, `subtitle`, `body`,
`subtext`, 최대 세 개의 `actions`를 지원한다.

Source: [Slack Card block](https://docs.slack.dev/reference/block-kit/blocks/card-block/),
checked 2026-08-12.

Decision: Review Card의 세 action을 Card block 하나에 둔다. Result Card는 같은 block에서
`actions`를 생략한다.

### R-002 Message Fallback

Block message는 top-level `text`를 notification과 accessibility fallback으로 사용할 수 있다.
Message는 최대 50 blocks를 지원한다.

Source: [Slack message composition](https://docs.slack.dev/messaging/formatting-message-text/),
checked 2026-08-12.

Decision: 모든 Card payload에 짧은 top-level `text`를 넣는다. 이 feature는 block 하나만
사용한다.

### R-003 Button Contract

Button element는 `action_id`, `value`, `style`, `confirm`을 지원한다. `value` 최대 길이는
2,000자이고 `action_id` 최대 길이는 255자다.

Source: [Slack button element](https://docs.slack.dev/reference/block-kit/block-elements/button-element/),
checked 2026-08-12.

Decision: Existing action IDs `approve`, `request_changes`, `reject`를 유지한다. Button value는
existing parser contract인 `{token_id}.{raw_credential}`를 사용한다. 각 action에 confirm을 둔다.

### R-004 Interaction Deadline

Slack interaction endpoint는 payload 수신 후 3초 안에 HTTP `200`을 반환한다. 지연 작업은
acknowledgement 뒤에 실행한다.

Source: [Slack interaction handling](https://docs.slack.dev/interactivity/handling-user-interaction/),
checked 2026-08-12.

Decision: `BoundedIngressAck`를 변경하지 않는다. Background `IngressDecisionWorker`가 decision과
safe feedback을 처리한다.

### R-005 Ephemeral Feedback

`chat.postEphemeral`은 channel 안의 한 user에게만 message를 보낸다. Delivery는 best effort다.

Source: [Slack chat.postEphemeral](https://docs.slack.dev/reference/methods/chat.postEphemeral/),
checked 2026-08-12.

Decision: Terminal denial/expired/stale/already-completed/unavailable feedback에 사용한다. Governance result나
retry source of truth로 사용하지 않는다.

## Repository Facts

### R-006 Semantic Projection

`DecisionProjectionPayload`는 decision identity와 revisions만 담는다. `text`와 `blocks`는 없다.
`SlackProjectionDestination`은 현재 semantic payload를 그대로 transport에 넘긴다.

Decision: Semantic event와 provider presentation을 분리한다. Outbox payload와 digest는 그대로
두고 Slack destination 안에서 message payload를 렌더링한다.

### R-007 Existing Reconciliation

Slack delivery는 message metadata marker의 `event_id`, `destination_ref`,
`destination_sequence`, `payload_digest`로 remote receipt를 찾는다. 판정 불가 시 hold하고,
history 소진으로 미전송이 확정된 경우만 재전송한다.

Decision: Review Card도 같은 marker와 destination sequence를 사용한다. Action set 발급은
`reconcile()`가 `None`을 반환한 뒤에만 실행한다.

### R-008 Existing ActionToken

`DecisionService.issue_tokens()`는 action별 raw credential을 한 번 반환하고 durable store에는
hash, token ID, Proposal snapshot, actor, channel, expiry만 저장한다. Existing default TTL은
15분이다.

Decision: Review Card 전용 TTL은 24시간이다. Transaction-scoped private helper를 추가해 token
발급과 action-set generation 기록을 atomic하게 만든다.

### R-009 Existing Signed Ingress

`SlackBlockActionAuthenticator`는 Slack signature, app/workspace, actor, channel, action ID,
button value를 검증한다. Ingress는 raw body digest와 raw credential hash만 저장한다.

Decision: Parser와 signed ingress를 재사용한다. Review Card renderer가 만드는 payload contract를
기존 parser가 소비하도록 한다.

### R-010 Existing Decision Worker

`IngressDecisionWorker`는 ack 뒤에 authority를 다시 확인하고 `DecisionService`를 호출한다.
Terminal error는 recovery hold로 분류된다. Worker result는 secret-free다.

Decision: Optional safe feedback port를 transaction 밖에서 호출한다. Feedback failure는 decision
또는 ingress terminal state를 되돌리지 않는다.

### R-011 Definition Shape

Immutable `ProposalDefinitionManifest.operations`는 JSON object tuple이다. Domain Proposal operation은
`type`과 `title`을 소유한다.

Decision: Review request 시 immutable definition을 읽어 `type`과 `title`만 strict하게 추출한다.
Reason, evidence, draft path, local path는 projection에 넣지 않는다.

### R-012 Schema Version

이 feature 시작 시 governance schema migration은 version `30`이었다.

Decision: Version `31`은 Request command와 action-set generation을 추가한다. Review에서 확인된
cross-channel duplicate를 기존 migration rewrite 없이 닫기 위해 version `32`가 immutable
snapshot 전체에 one-command unique index를 추가한다. `verify_schema()` exact shape 검사를 함께
갱신한다.

## Alternatives

### A-001 Store Raw Actions In Outbox

Rejected. Outbox `payload_json`은 durable이고 exception/debug dump에도 노출될 수 있다.
FR-018을 위반한다.

### A-002 Issue Tokens Before Enqueue

Rejected. Process가 enqueue 뒤 send 전에 재시작하면 raw credential을 회수할 수 없다. 반복
enqueue는 duplicate review intent를 만든다.

### A-003 Edit Original Review Card

Rejected for v1. `chat.update`는 visible history와 reconciliation contract를 넓힌다. Separate
Result Card가 더 작고 기존 ordered outbox와 맞는다.

### A-004 Add Interaction Server

Rejected. Repository constitution이 central server와 새 auth surface의 임의 추가를 금지한다.
Signed local integration과 existing endpoint boundary까지만 검증한다.

### A-005 Use `response_url` As Durable Feedback

Rejected. Current ingress는 `response_url`을 보존하지 않는다. URL은 authority-bearing delivery
capability이므로 durable secret 정책과 lifecycle을 새로 요구한다.

## Resolved Decisions

| ID | Decision |
|---|---|
| D-001 | Official `card` block 하나와 top-level fallback `text` 사용 |
| D-002 | Semantic outbox payload와 Slack presentation payload 분리 |
| D-003 | Result Card 먼저 구현 |
| D-004 | Review Card request는 reviewed transition 뒤 별도 idempotent command |
| D-005 | Token raw value는 `send()` attempt 메모리 전용 |
| D-006 | Retry마다 remote marker를 먼저 reconcile |
| D-007 | Confirmed absent일 때만 old set revoke 후 fresh set issue |
| D-008 | Remote ambiguity는 replacement 없이 operator hold |
| D-009 | One reviewer, 24-hour TTL, three actions |
| D-010 | Safe ephemeral feedback은 non-authoritative best effort |
