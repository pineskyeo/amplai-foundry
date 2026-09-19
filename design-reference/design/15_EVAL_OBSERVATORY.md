# 15. Eval Observatory·추적·측정

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 시작부터 관측한다

Observatory를 메타하네스 직전의 부가 dashboard로 두지 않는다. 첫 V3 Work부터 RunRecord·구조화 event·artifact/verdict 연결을 기록한다. 이것은 후속 자동 최적화의 전제이며, 화면보다 신뢰할 수 있는 데이터 계약이 먼저다. trace가 비어 있는데 성공률만 표시하지 않는다. [R10,R15]

RunRecord는 scope/root_goal/contract_digest/graph_digest/node/run/parent run/model/driver/sandbox/harness composition/context bundle/skills/tools/usage/latencies/failure signatures/human interventions/acceptance coverage/verdict/effect refs를 담는다. private chain-of-thought 대신 **관측 가능한 action, 짧은 판단 이유, 근거 ref**를 남긴다.

## 2. 이벤트와 trace 구조

trace root는 goal attempt, span은 resolver/compiler/dispatch/tool/verifier/human gate/replan/meta experiment다. 실행 event seq는 Runtime Store가 부여하고 OTel timestamp에 의존하여 authoritative order를 추론하지 않는다. wall time과 duration을 구분하며 duration은 monotonic clock으로 계산한다. delayed event는 event_at와 ingested_at를 모두 갖는다.

OpenTelemetry exporter는 내부 RunRecord의 projection이다. GenAI semantic conventions의 별도 저장소 이동/버전을 고려해 mapping version을 pin한다. provider-specific attribute를 무분별하게 internal schema에 퍼뜨리지 않는다. OTel collector 장애 시 실행 권한이 바뀌지 않으며 locally bounded spool 후 drop accounting을 남긴다. 중요한 authority/effect event는 telemetry drop 대상이 아니다. [R28,R29]

## 3. Metric 정의

| metric | 분자 / 분모·범위 | 오해 방지 |
|---|---|---|
| verified goal rate | 모든 mandatory acceptance 충족 goal / 종료된 eligible goal | 취소·blocked·inconclusive를 숨기지 않고 별도 표시 |
| first-attempt verified rate | repair 없이 verified / eligible | 쉬운 업무 증가와 구분해 class별 stratify |
| acceptance coverage | trusted verdict가 있는 mandatory criteria / 전체 mandatory | waived는 pass 아님 |
| rework | verifier failure 후 재실행 effort | 모델 token만이 사람 effort는 아님 |
| human intervention | 질문/승인/steer unique event와 active wait | 자가보고 생산성 지표로 대체하지 않음 |
| lead time | intent 접수→verified; queue/compute/human wait 분해 | background 대기와 실제 작업량 구분 |
| cost | provider 확정 usage 또는 bounded estimate/unknown | 미보고 usage를 0원으로 하지 않음 |
| reliability | lost lease/duplicate suppressed/unknown effect/recovery latency | retry로 감춘 실패도 집계 |
| context quality | stale refs/conflicts/missing invariant incidents | token 수 감소만 최적화하지 않음 |
| safety | denied/revoked/escaped/secret incidents | 0건 관측≠위험 0 |

보고는 model/driver/pack/harness version, task class, risk, repo, time window로 slice한다. 적은 표본에는 count와 uncertainty를 함께 표시한다. 모든 대상에 하나의 성공점수를 적용하지 않는다.

## 4. Dataset 관리

기존 회귀 tests, 실제 실패를 sanitized/replayable case로 만든 corpus, domain goldens, adversarial/safety cases를 분리한다. corpus version, license, privacy class, expected outcome, verifier version, task difficulty, environment fingerprint를 manifest로 고정한다. 신규 사건이 holdout에 들어갈 때 이미 proposer에게 노출된 정보는 contamination 표시한다.

train/development/validation/holdout 역할을 명시한다. meta proposer는 development failure digest만 보고 hidden holdout 원문·expected outputs에 접근하지 않는다. case를 삭제하거나 난이도를 낮추는 변경은 benchmark governance로 별도 review한다. 전부 공개된 로컬 프로젝트에서는 진짜 숨긴 holdout이 없을 수 있으므로 'independent holdout'이라고 잘못 표기하지 않는다.

## 5. Eval 실행 모드

`static`: schema/permission/config analysis. `replay`: immutable input + tool observations로 순수 로직 재현. `sandbox_rerun`: 실제 모델/도구를 새 격리 환경에서 실행. `shadow`: 실사용 입력으로 결과만 비교하고 external effects 금지. `canary`: 명시적으로 허용된 일부 업무에서 신규 composition 실행. 이 다섯 모드를 결과에서 구분한다. 기록된 trajectory를 재생했다고 live 성공률이 증명되는 것은 아니다.

external system을 수반하는 test는 simulator/contract stub→staging integration 순서로 검증하며 production mutation을 eval용으로 사용하지 않는다. secrets·customer source는 export 전에 policy scrub을 적용한다. synthetic fixture에는 실제 operation/승인 효력이 없다는 표시를 둔다.

## 6. 통계와 판정

각 실험은 objective, primary endpoint, baseline/candidate, task sampling, strata, paired design, repeat policy, non-inferiority margin, safety stop, cost ceiling, analysis rule을 **실행 전에 고정**한다. 항상 n=30 또는 임의의 95% 숫자로 통과시키지 않는다. baseline variance와 허용 오차에 맞춰 표본·반복을 정하고 예산 부족 시 `inconclusive`로 끝낼 수 있다.

paired task comparisons와 반복 실행을 사용하되 동일 seed가 모델의 결정성을 보장하지 않는다는 점을 기록한다. 많은 candidate 중 최고만 골라 같은 holdout을 반복 사용하지 않는다. confirmatory test와 탐색적 비교를 분리한다. sequential canary 중간 확인은 사전 정의 stop rule 또는 적절한 error control을 따른다. 배포 결정은 통계 점수만이 아니라 보호 gate와 authority를 모두 요구한다.
