# Implementation Plan: AMPLAI V3 RC-02 — Catalog Closure

**Created**: 2026-09-19

## 실행 방식

오케스트레이터(이 세션)가 slice 를 배정하고 sonnet 서브에이전트가 test 파일 하나씩을 소유한다. src 는 서브에이전트에 읽기 전용이다. 동시 실행은 3개까지다.

| wave | slices | 방식 |
|---|---|---|
| 1 | S01 S02 S03 | 병렬 3 |
| 2 | S04 S05 S06 | 병렬 3 |
| 3 | S07 S08 | 병렬 2 (S08 은 src 수정이므로 오케스트레이터가 직접) |
| 4 | S09 | 직렬, 오케스트레이터 |

## 게이트

각 slice 는 `scripts/eval.sh --feature specs/014-v3-catalog-closure --slice <ID>` 로 판정한다. 전체는 verifier profile v2 다.
