---
schema_version: 1
id: ARC-0007
namespace: org/default/project/amplai
project: amplai
kind: architecture
status: candidate
title: Messenger Proposal Control은 channel adapter와 AMPLAI authority를 분리한다
summary: Channel-independent ProposalAction을 Slack, Telegram, Hermes와 AMPLAI control boundary 사이의 공통 계약으로 둔다.
created_at: 2026-07-29
updated_at: 2026-07-29
source_refs: [SRC-20260729-B13BC5DA]
relations:
  - {type: implements, target: PRI-0003}
  - {type: related_to, target: ARC-0003}
  - {type: related_to, target: CON-0010}
  - {type: related_to, target: CON-0012}
  - {type: related_to, target: DEC-0007}
revision: 1
tags: [architecture, messenger, proposal, authority, adapter]
---
# Messenger Proposal Control은 channel adapter와 AMPLAI authority를 분리한다

## Boundary

Slack과 Telegram은 Proposal Card를 표시하고 interaction을 받는 adapter다. Hermes는 사용자의 자연어와 파일을 `IntentRequest`로 구조화하고 AMPLAI API에 제출하는 Client Partner다.

AMPLAI는 channel-independent `ProposalAction`을 받는다. 이 계약은 qualified Proposal identity, expected version·digest, action, actor, channel, opaque action token, idempotency key와 발생 시각을 포함한다.

AMPLAI는 Project와 Actor authority, Proposal의 현재 version·digest, 허용 상태 전이, replay와 audit 조건을 다시 검사한다. Slack Block Kit, Telegram Inline Keyboard, Web UI와 CLI는 같은 core action 의미를 각 채널 형식으로만 render한다.

## Dependency Gate

외부 channel의 실제 승인과 apply 연결은 Action Token 일회성 소비, provider request 검증과 durable audit가 준비된 뒤 연다. 그 전에는 core contract와 read-only card integration만 허용한다.
