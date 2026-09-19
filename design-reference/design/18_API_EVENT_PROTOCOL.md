# 18. API·Command·Event Protocol

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 계약과 transport

V3 application protocol major는 3, schema release는 3.0.0으로 제안한다. endpoint prefix는 `/api/v3`. 기존 Platform API/CLI는 adapter로 공존하며 implicit reinterpretation하지 않는다. 내부 command는 API·CLI·Hermes에서 동일한 application service로 수렴한다. 외부 요청이 DB/파일을 직접 수정하지 못한다.

HTTP JSON은 UTF-8, strict JSONSchema2020-12 + semantic validation. schema의 `$id`는 offline registry key이며 runtime이 인터넷에서 임의 `$ref`를 받아오지 않는다. 구조화 생성용 provider subset schema와 normative schema를 구분하고 모델 출력은 **최종 normative validator를 반드시 거친다**. [R32]

## 2. 공통 header/조건

`Authorization`은 project/tenant binding을 가진 서버 검증 credential. 클라이언트 JSON의 actor 필드는 attribution일 뿐 실제 ActorContext를 대체하지 않는다. write commands는 `Idempotency-Key`, 변경 가능한 aggregate update는 `If-Match` row version을 사용한다. 서버가 contract digest와 policy epoch를 추가 확인한다. 요청 payload digest와 key binding을 보관한다.

모든 response는 request_id, server_time, schema_version, result 또는 typed error를 가진다. error는 code/message/retryable/retry_after/hold_reason/current_revision/safe_next_action을 제공하며 secret·raw provider prompt를 포함하지 않는다. error 문자열을 parsing해서 상태를 정하지 않는다.

## 3. Endpoint inventory

정본은 `contracts/api-catalog.json`. 경로의 `{goal_id}`도 auth scope 내부에서 조회한다. 다음은 핵심 그룹이다.

| route | 의미 | 성공 의미 |
|---|---|---|
| POST /intents | 의도 접수 | 202 persisted, 실행 승인 아님 |
| POST /intents/{id}/resolve | discovery job 요청 | resolution job 접수 |
| GET /goals/{id} | 현재 goal projection | scope 안의 상태 |
| POST /goals/{id}/contracts | frozen contract 후보 생성 | 생성됨, active 아님 |
| POST /goals/{id}/activate | current contract/graph 활성화 | gates 통과 후 admission 가능 |
| POST /goals/{id}/steering | 중간지시·pause/resume/cancel | ledger 접수; 실제 적용 별도 |
| POST /questions/{id}/answers | 인증된 answer | validation 및 contract 반영 별도 |
| POST /graphs/{id}/revisions | replan draft 등록 | semantic valid draft |
| POST /workers/claim | admission 통과 work claim | fenced lease, run envelope |
| POST /runs/{id}/heartbeat | lease 연장 | exact lease+fence 유효 시만 |
| POST /runs/{id}/events | worker 관측 event | 서버가 허용된 event만 채택 |
| POST /artifacts/init + commit | artifact ingest | registry가 hash/scan 검증 |
| POST /effects | broker effect 준비·요청 | receipt 상태, 완료와 구분 |
| POST /verifications | trusted verifier 실행 요청 | verification job |
| POST /meta/proposals | 후보 제안 | 권한/배포 부여 아님 |
| POST /meta/experiments | 승인된 eval freeze/start | experiment state |
| POST /releases/{id}/promote | exact grant 기반 promote | pointer CAS + rollout receipt |
| GET /events | resumable stream | projection; authoritative command ACK 아님 |

승인/ApplyGrant issuance는 기존 governance API의 adapter를 사용하며 worker-facing endpoint에 자기 grant 발급 기능을 넣지 않는다. 파일 ref는 local absolute path 대신 registry ID+digest이며 artifact content download도 scope/분류를 검증한다.

## 4. Event envelope와 ordering

필드: event_id, protocol_version, scope, aggregate_type/id, aggregate_seq, event_type, producer, causal_command_id, correlation_id, occurred_at, ingested_at, payload, payload_digest. aggregate_seq는 authority 있는 서버만 부여한다. worker가 `goal.verified`/`grant.issued` 같은 server-only event를 제출하면 reject한다.

worker events에는 lease_id, fencing_token, contract_digest, graph_digest, work_attempt가 있어야 한다. state transition은 payload 설명이 아니라 현재 aggregate 상태+guards로 수행한다. 알려지지 않은 event type은 quarantine/unsupported; 조용히 무시하고 success로 처리하지 않는다.

SSE는 `Last-Event-ID` 또는 scoped cursor로 재개한다. retention 밖 cursor는 `CURSOR_EXPIRED` + snapshot endpoint를 반환한다. 여러 aggregate event의 timestamp로 total order를 재구성하지 않는다. client projection이 gap을 발견하면 snapshot+after cursor로 복구한다. WebSocket은 adapter 선택사항이며 기본 runtime durability가 socket 연결에 달려 있지 않다.

## 5. 오류 코드와 재시도

400 VALIDATION_ERROR, 401 AUTHENTICATION_REQUIRED, 403 SCOPE_DENIED/CAPABILITY_DENIED, 404 scoped NOT_FOUND, 409 REVISION_CONFLICT/IDEMPOTENCY_CONFLICT/STALE_LEASE, 412 CONTRACT_STALE/GRANT_STALE, 422 GRAPH_INVALID/ACCEPTANCE_UNBOUND/AMBIGUOUS_TARGET, 429 BUDGET_EXHAUSTED/RATE_LIMITED, 503 AUTHORITY_UNAVAILABLE/PROVIDER_UNAVAILABLE. budget exhausted는 시간이 지나면 자동 해결되는 rate limit과 구분한다.

unknown external outcome은 HTTP retryable=true로 노출하지 않는다. 먼저 `GET effect receipt` 또는 reconcile command를 요구한다. 202/204나 queue ACK는 verified를 뜻하지 않는다.

## 6. 버전 변경

schema minor 확장은 optional field일 때만 호환일 수 있다. strict consumers에는 허용된 version negotiation/adapter가 필요하며 모르는 필드를 무조건 받지 않는다. protocol major unsupported는 handshake 단계에서 fail closed다. breaking state semantics는 migration+conformance+release matrix를 요구한다. vendor protocol은 AMPLAI protocol과 version을 따로 기록한다.
