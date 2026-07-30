# Current Item — MGC-007

## Goal

CLI와 Intake를 포함한 production mutation entrypoint가 governed Decision·Authority 경계를
우회하지 못하게 하고, Apply 구현 전까지 직접 적용 경로를 fail-closed한다.

## Frozen Acceptance

- A1: Proposal decision·status·apply를 변경하는 production call site inventory가 코드와 test 기준으로 고정됨
- A2: CLI·Intake·public request가 governance `AuthorityContext` 또는 permission을 직접 생성·주입하지 못함
- A3: `approve`, `reject`, `request_changes` mutation이 live `AuthorityService`를 사용하는 `DecisionService`로만 수행됨
- A4: unregistered·disabled·Project permission 없는 Actor의 CLI·Intake mutation이 fail-closed함
- A5: Intake Policy Actor는 `proposal.submit_review`까지만 수행하고 decision·apply를 실행하지 못함
- A6: production code에서 `approve_proposal()` 직접 호출이 decision boundary 밖에 존재하지 않음
- A7: `ProposalApplyService` 또는 동등한 direct apply entrypoint가 CLI·Intake·public API에 노출되지 않음
- A8: Apply 요청은 MGC-009 ApplyGrant 경계가 준비될 때까지 명시적 deferred/denied 결과를 반환함
- A9: Governance Store unavailable·Authority resolution failure 시 legacy YAML·Proposal·Token·Git state가 변경되지 않음
- A10: architecture test가 forbidden import/call과 caller-created governance AuthorityContext의 재도입을 탐지함
- A11: 기존 CLI·Intake read/submit flow의 호환 가능한 부분과 전체 regression test가 통과함
- A12: `amplai-foundry verify`가 통과함
- A13: Subagent review P0/P1/Blocking-P2 0건

## In Scope

- CLI와 Intake production mutation inventory
- Legacy `ProposalActionService` caller-created AuthorityContext 경로 폐쇄
- Decision mutation의 `AuthorityService` + `DecisionService` routing
- Intake Policy Actor submit-only enforcement
- Direct `approve_proposal`와 direct apply exposure 제거
- Fail-closed compatibility error와 architecture regression test

## Out Of Scope

- Audit hash chain과 projection outbox → `MGC-008`
- ApplyGrant와 Apply Job 구현 → `MGC-009`
- Fenced canonical publish → `MGC-010`
- Slack·Telegram Provider adapter → `MGC-012`, `MGC-013`
- Runtime activation UI와 kill switch → `MGC-015`

## Review Team

- Direct mutation and Authority injection reviewer subagent
- CLI/Intake boundary reviewer subagent
- Failure and architecture-test evidence reviewer subagent
