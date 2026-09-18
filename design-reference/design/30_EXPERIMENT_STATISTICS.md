# 30. Harness 실험의 판정 규약

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 실험 단위

task(case)가 primary unit이며 repeated run은 task 내 stochastic variation을 추정한다. baseline/candidate에 동일 task/environment/entry constraints를 배정한다. 여러 agent output을 서로 독립 task sample처럼 세어 표본 수를 부풀리지 않는다. latency는 queue/compute/human wait를 구분하고 비용은 measured/estimated/unknown 비율을 공개한다.

## 2. 사전 규칙

ExperimentPlan은 primary endpoint, quality non-inferiority margin(있다면), benefit endpoint, baseline variance estimate, repeat/sample rationale, failure/aborted/inconclusive treatment, missing data policy, confidence/analysis procedure, sequential stopping, subgroup plan, maximum compute budget을 선언한다. 값을 설정하지 못한 실험은 exploration으로 표시하며 자동 promotion의 confirmatory evidence로 쓰지 않는다.

## 3. 판정 우선순위

1. scope/authority/evidence/safety integrity 위반은 점수와 무관하게 fail/abort다.
2. 환경 drift·오염·missing usage/결과가 비교 가능성을 훼손하면 inconclusive다.
3. 충분한 evidence와 사전 분석 규칙에 따라 quality·benefit·cost 기준을 비교한다.
4. canary와 final promote는 별도의 authority·rollback gate를 요구한다.

사전 non-inferiority를 선택했다면 '차이가 유의하지 않다'를 '같다'라고 바꾸지 않는다. uncertainty interval이 허용 경계를 넘으면 증거 부족이다. 여러 endpoint 중 유리한 것만 사후 primary로 선택하지 않는다.

## 4. 실행 권장안

paired task-level differences, task-level bootstrap 또는 데이터 특성에 맞는 분석법을 구현자가 통계 리뷰와 함께 선택한다. 반복치가 묶인 구조를 보존하고 dependency를 무시하지 않는다. API seed/pin은 재현에 도움을 주지만 완전 결정성을 보장하지 않는다. 연구 논문의 특정 성공률/비용절감률을 AMPLAI의 목표치로 복사하지 않는다.

safe canary 동안 언제나 monitoring할 수 있지만 같은 데이터로 원하는 p-value가 나올 때까지 멈추지 않는 방식은 금지다. 사전 stop rule과 exploratory/confirmatory 구분을 남긴다. 여러 candidate는 development corpus에서 고르고 최종 검증 corpus의 반복 사용을 제한한다.

## 5. 보고 형식

대상 task class/표본 task 수/총 runs/제외 사유/버전/env/metric 정의/baseline와 candidate 결과/불확실성/안전 사건/비용 미보고분/한계/권고를 함께 표시한다. 최종 verdict는 pass/fail/inconclusive/aborted다. tradeoff가 있는 경우 비용 절감만 강조하지 않고 사용자가 승인한 objective와 어떻게 맞는지 설명한다.

이 규약은 특정 통계 수치를 이번 설계에서 계산했다는 뜻이 아니다. 실제 baseline 수집·power/sample planning은 다음 구현의 Evaluation work다.
