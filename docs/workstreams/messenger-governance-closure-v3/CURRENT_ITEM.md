# Current Item — MGC-005

## Goal

Provider 인증이 끝난 command를 raw secret 없이 durable ingress에 commit한 뒤에만 성공 ack한다.
Worker는 atomic lease와 bounded retry로 command를 처리하고 crash 후 안전하게 reclaim한다.

## Frozen Acceptance

- A1: Ingress entrypoint가 raw size 제한과 Provider authenticity를 durable write 전에 검증함
- A2: durable ingress가 raw request body와 raw Token을 저장하지 않고 body digest와 credential hash만 저장함
- A3: Provider installation과 immutable external event scope를 포함한 fingerprint로 duplicate command를 식별함
- A4: 신규 ingress가 commit된 뒤에만 accepted ack 결과를 반환함
- A5: DB unavailable 또는 busy timeout이면 accepted로 표시하지 않고 3초 이내 non-success 결과를 반환함
- A6: Ingress state가 `pending → leased → completed | retry_wait | recovery_hold`, `retry_wait → leased → dead_letter` 전이만 허용함
- A7: claim이 lease owner, expiry, monotonic generation과 attempt를 한 SQLite transaction에서 갱신함
- A8: 만료된 lease를 다른 Worker가 reclaim하고 stale generation Worker의 finalize를 거부함
- A9: retry가 bounded attempt와 deterministic backoff를 적용하고 한도 도달 시 dead letter로 전이함
- A10: 동일 Provider fingerprint replay가 기존 command ID와 ack 결과를 반환하고 row를 중복 생성하지 않음
- A11: verified credential hash를 사용하는 connection-bound decision integration seam을 제공하고 raw Token 재저장을 요구하지 않음
- A12: ingress transaction과 worker claim/finalize에 Provider network 또는 filesystem I/O가 없음
- A13: hard-kill, busy/unavailable, replay, lease reclaim, stale finalize와 raw-secret absence test가 통과함
- A14: existing test와 `amplai-foundry verify` 통과
- A15: Subagent review P0/P1/Blocking-P2 0건

## In Scope

- Provider-independent verified ingress command model
- Schema v4 ingress queue와 fingerprint uniqueness
- Commit-before-ack application service
- Atomic lease claim, reclaim, generation fencing와 attempts
- Retry wait, deterministic backoff, recovery hold와 dead letter
- Credential-hash decision seam
- Failure, hard-kill, concurrency와 raw-secret absence test

## Out Of Scope

- Slack request signature implementation → `MGC-012`
- Telegram webhook secret implementation → `MGC-013`
- Actor binding과 server-created AuthorityContext → `MGC-006`
- Production CLI/Intake mutation 경로 폐쇄 → `MGC-007`
- Audit/outbox projection → `MGC-008`
- Provider-specific message rendering과 ack transport → `MGC-012`, `MGC-013`

## Review Team

- Ingress authenticity and secret-minimization reviewer subagent
- Lease, retry and crash-recovery reviewer subagent
- Transaction and evidence reviewer subagent
