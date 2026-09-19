# Feature Specification: AMPLAI V3 RC-02 — Catalog Closure

**Feature Branch**: `feat/013-amplai-v3` (RC-01 위에 이어서)

**Created**: 2026-09-19

**Status**: Ready for implementation

**Input**: 사용자 지시 "너가 오케스트레이션하고 병렬로 진행해서 완성해줘" + `release/rc01-conformance-closure.json` 의 not_run 64 케이스

## Scope

RC-01 (`specs/013-amplai-v3`) 은 catalog 112 케이스 중 48 을 local_pass 로 묶었다. 이 Work 는 남은 64 를 그룹별로 나눠 실행 증거를 만든다.

| slice | group | 케이스 |
|---|---|---|
| S01 | graph | T-021, T-022, T-023, T-024, T-025, T-026, T-028, T-030 |
| S02 | runtime | T-031, T-032, T-033, T-034, T-035, T-036, T-037, T-038 |
| S03 | effects | T-044, T-045, T-046, T-047, T-048, T-049, T-050, T-051 |
| S04 | goal | T-007, T-008, T-009, T-011, T-012, T-013, T-014 |
| S05 | lease, evidence | T-039, T-040, T-041, T-042, T-043, T-052, T-053, T-054, T-055, T-056 |
| S06 | meta, meta decision, canary promote | T-079, T-080, T-081, T-082, T-083, T-084, T-085, T-086, T-088, T-091 |
| S07 | driver, visual, security, release conformance | T-064, T-065, T-066, T-067, T-068, T-069, T-070, T-071, T-072, T-058, T-076, T-108, T-109 |
| S08 | review round 2 follow-ups | R102 R106 R107 R108 |
| S09 | closure | ledger 재생성 |

## 정본

설계 정본은 `design-reference/` 이며 무변경이다. 각 케이스의 given/when/expected 는 `design-reference/eval/test-catalog.json` 에 있고 이 spec 은 그것을 다시 쓰지 않는다.

## 하지 않는 것

live provider/컨테이너/holdout/실제 브라우저 자격 인증, V3 FINAL 선언, push/merge/release. 외부 시스템이 필요한 케이스는 fake pass 하지 않고 `external-boundary.json` 에 사유를 남긴다.
