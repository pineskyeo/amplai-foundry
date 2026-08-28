---
name: systematic-debugging
description: Internal failure-diagnosis capability for dev-loop. Reproduce, narrow, identify a stable failure signature and root cause, then decide whether the problem belongs to implementation, task, plan, spec, requirement, or environment.
user-invocable: false
disable-model-invocation: false
---

# Systematic Debugging

repair를 반복하기 전에 **어디서 처음 틀어졌는지**를 증거로 좁힌다.

## Procedure

1. **Observe** — 원문 error, exit code, failing check/test, selected Context Pack, input/environment fingerprint를 보존한다.
2. **Reproduce** — 가장 작은 명령으로 같은 실패를 다시 만든다. 재현 불가면 추정 fix 금지.
3. **Narrow** — 정상/비정상 경계를 파일·함수·event·artifact 단계로 이분한다.
4. **Hypothesis** — 한 번에 하나의 원인만 세우고 반증 가능한 관찰을 정한다.
5. **Instrument** — 기존 로그/fixture로 부족할 때만 임시 계측한다. production data는 바꾸지 않는다.
6. **Root cause** — 증상과 원인을 분리하고 path:line/evidence로 고정한다.
7. **Minimal repair** — 원인 지점만 수정하고 임시 계측을 제거한다.
8. **Verify** — 원래 reproduction과 relevant regression을 다시 실행한다.

## Required output

```text
FAILURE SIGNATURE
- command/check:
- stable symptom:
- first bad boundary:
- root cause evidence:
- attempted repairs:
- classification: IMPLEMENTATION | TASK | PLAN | SPEC | REQUIREMENT | ENVIRONMENT | KNOWLEDGE
- next action:
```

검증을 다시 실행하지 않았으면 `fixed`라고 말하지 않는다. REQUIREMENT는 사람에게,
ENVIRONMENT는 blocked evidence로 올린다. KNOWLEDGE는 coding을 멈추고 readiness/discovery/context를 다시 구성한다.
