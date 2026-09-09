# Lifecycle

## States

| Status | Meaning |
|---|---|
| `candidate` | 검토 중인 공식화 후보 상태 |
| `active` | 현재 유효한 공식 지식 |
| `superseded` | 새 지식이 대체한 상태 |
| `deprecated` | 사용 중단을 권고하는 상태 |
| `merged` | 다른 memory에 의미를 합친 상태 |
| `rejected` | 검토 후 채택하지 않은 상태 |
| `archived` | 현재 탐색 기본 범위에서 제외한 상태 |

## Invariants

- Source는 ingest-only immutable record다. Proposal은 Source를 생성하거나 변경하지 않는다.
- `superseded`는 `superseded_by`가 필요하다.
- `merged`는 `merged_into`가 필요하다.
- Active 공식 지식은 `source_refs`가 필요하다. `source` note는 예외다.
- `decision`이 `active` 또는 `superseded`이면 `## 결정` 또는 `## Decision` 본문이 필요하다.
- 해결되지 않은 `question`은 `active`다.
- 종료된 `question`은 관련 decision 또는 knowledge relation이 필요하다.
- `90-archive` directory의 note는 `archived`여야 한다.
- Source는 `superseded` 상태와 `superseded_by`를 사용하지 않는다.
- `supersedes` relation과 대상의 `superseded_by`는 양방향으로 일치한다.

## Allowed Transitions

| Before | Allowed after |
|---|---|
| `candidate` | `candidate`, `active`, `rejected`, `archived` |
| `active` | `active`, `deprecated`, `superseded`, `merged`, `archived` |
| `deprecated` | `deprecated`, `active`, `superseded`, `archived` |
| `superseded` | `superseded` |
| `merged` | `merged` |
| `rejected` | `rejected` |
| `archived` | `archived` |

`superseded`, `merged`, `rejected`, `archived`는 terminal status다. 현재 정책은 terminal status의 복구 또는 재활성화를 허용하지 않는다.

`UPDATE`, `LINK`, `SUPERSEDE`는 ID, project, namespace, kind, created_at을 유지한다. Revision은 정확히 1 증가하고 updated_at은 이전 값보다 같거나 늦어야 한다.

## Replacement

새 Decision B가 이전 Decision A를 대체하면 B에 `supersedes -> A`를 추가한다. A는 `status: superseded`와 `superseded_by: B`를 기록한다. 두 변경을 같은 review 단위에서 검증한다.

## Runtime Work Status

Project Store Work의 `DRAFT`, `WAITING`, `READY`, `RUNNING`, `HUMAN_REQUIRED`, `FAILED`, `DONE`는 canonical
Knowledge lifecycle과 별개다. Slack status는 append-only Work event에서 파생된 outbox projection이며,
Slack delivery 결과가 Work 상태나 canonical knowledge lifecycle을 변경하지 않는다.

`DRAFT` Work의 Activation Card도 별도 durable outbox에서 전송한다. 이 outbox는 승인된
사람·project·provider·feature·revision·digest와 Slack receipt만 보관한다. raw action token은
전송 worker가 직전에 발급하고 hash-only activation ledger에만 남긴다. 전송 직후 process가
중단되면 Slack marker를 읽어 같은 카드를 다시 보내지 않는다. Hermes가 전달한 user ID는 이
card의 authority가 아니며, AMPLAI Slack App의 signed interaction과 Actor binding이 authority를
확정한다.
