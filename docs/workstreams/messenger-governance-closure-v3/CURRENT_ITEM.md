# Current Item — MGC-006

## Goal

외부 Provider identity를 governed Actor binding으로 해석하고, production mutation에 사용할
`AuthorityContext`를 서버가 단일 경로에서 생성한다.

## Frozen Acceptance

- A1: Production `AuthorityContext` 생성 경로가 `AuthorityService.authenticate()` 하나로 제한됨
- A2: Public request가 permission 또는 AuthorityContext를 주입할 수 없음
- A3: 외부 identity가 Provider, installation/workspace와 immutable external actor ID의 composite key로 저장됨
- A4: active external identity binding이 유일하며 silent overwrite를 거부함
- A5: binding create/rebind/disable이 explicit approval, reason과 before/after diff를 요구함
- A6: rebind가 기존 active binding disable과 새 binding activation을 한 SQLite transaction으로 수행함
- A7: binding transition record가 append-only이며 승인자, 사유, 이전·이후 Actor를 보존함
- A8: unmapped·disabled Actor와 Project permission 부재가 fail-closed함
- A9: decision permission은 human Actor에게만 부여하고 Service/Agent decision을 거부함
- A10: Intake Policy Actor 권한이 `proposal.read`, `proposal.submit_review`로 제한됨
- A11: pending ingress 처리 시 binding과 Project permission을 다시 조회해 Authority를 생성함
- A12: Actor/Authority 실패가 Proposal, Token과 ingress decision result를 변경하지 않음
- A13: binding race, rebind rollback, disabled/unmapped, cross-Project permission과 human-only test가 통과함
- A14: existing test와 `amplai-foundry verify` 통과
- A15: Subagent review P0/P1/Blocking-P2 0건

## In Scope

- Schema v5 Actor, external binding, permission policy와 transition record
- Server-created AuthorityService
- Approved binding create/rebind/disable commands
- Active identity uniqueness와 atomic rebind
- Project-scoped permission resolution
- Human decision와 Intake Actor policy
- Ingress execution 시 Authority 재평가 seam
- Failure, rollback, race와 cross-Project test

## Out Of Scope

- Audit hash chain과 projection outbox → `MGC-008`
- Direct CLI/Intake mutation closure → `MGC-007`
- Slack/Telegram Provider verification → `MGC-012`, `MGC-013`
- ApplyGrant permission execution → `MGC-009`
- Activation administration UI → `MGC-015`

## Review Team

- Authority injection and human-decision reviewer subagent
- Binding transition and race reviewer subagent
- Permission/failure evidence reviewer subagent
