# 16. Meta-Harness 전체 설계

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 두 책임을 나눈다

V3 Meta-Harness는 (A) **Federation & Composition**: 다양한 driver/model/pack/sandbox를 조합·추적·운영하는 층과 (B) **Evidence-driven Evolution**: 관측을 바탕으로 조합·규칙 변경을 제안하고 검증·배포하는 층으로 나눈다. 두 층 모두 Runtime의 권한·effect protocol을 우회하지 않는다. 'meta'는 무제한 최상위 agent를 뜻하지 않는다.

연구의 Meta-Harness/AHE는 반복 측정으로 harness 자체를 개선하는 가능성을 보여주는 근거다. benchmark 성능 증가를 AMPLAI 효과로 옮겨 적거나 자동 자기변경을 production에 바로 적용하는 근거로 쓰지 않는다. [R14,R15]

## 2. Composition Registry

`HarnessComposition`은 versioned immutable release object다. model profile·driver profile·sandbox profile·capability packs·prompt bundle·router policy·context policy·verification profile·budget policy·protocol compatibility를 정확한 digest로 고정한다. native subagent count 같은 optional 설정도 포함한다. 'latest' symbolic reference를 실행 중 해석하지 않는다.

Selection은 project/data/risk/required capabilities로 후보를 **먼저 필터링**한 뒤 task-class baseline policy로 선택한다. 모델 추천 agent는 후보를 제안할 수 있지만 허용되지 않은 외부 provider나 unsupported transport를 선택할 수 없다. dispatch 이후 composition은 고정되며 변경은 checkpoint + new attempt/steering revision이다. 자동 online bandit·강화학습 routing은 초기 기본값이 아니다. 충분한 관측과 안전한 실험 protocol 없이 live 최적화를 켜지 않는다.

## 3. 변경 가능 surface

| class | 예 | 제안/적용 정책 |
|---|---|---|
| A: low-risk composition | task별 prompt excerpt, approved pack 조합, bounded planning strategy | offline eval→low-risk canary→정책 내 승인된 release |
| B: behavior/runtime | routing threshold, context retrieval, repair heuristic, driver mapping | 독립 code review·fault/security regression·human promote |
| C: protected control | authority, verifier core, budget enforcement, signing, holdout | 일반 meta-autopromote 금지; 별도 governed engineering Work |
| D: forbidden action | 자기 approval 발행, evidence 조작, secret 유출, production 무단 access | schema/permission에서 reject |

검증기를 개선하는 합법적 작업도 가능하다. 다만 후보 harness 개선 실험과 동시에 evaluator를 바꾸어 효과를 비교하지 않는다. evaluator 변경은 별도 baseline qualification이고 이전 결과와 동등 비교 가능성을 검증해야 한다.

## 4. Evolution pipeline과 상태

```text
Observation cluster
 -> ChangeProposal(draft)
 -> screened
 -> experiment_approved
 -> offline_running
 -> offline_evaluated(pass | fail | inconclusive)
 -> canary_approved
 -> canary_running
 -> promotion_pending
 -> promoted | rejected | rolled_back
```

각 이동은 `contracts/state-machines.json`의 guards를 따른다. `fail/inconclusive`에서 promote로 바로 이동하지 않는다. 조기 중단은 aborted와 reason을 남긴다. MetaProposer는 development observations와 allowed surfaces만 받는다. proposal은 hypothesis, observed failure refs, exact candidate diff, expected benefit, expected risk, affected classes, protected-surface scan, evaluation plan, rollback plan을 반드시 담는다.

## 5. 실험 생성과 독립성

ExperimentService가 baseline/candidate/corpus/verifier/environment/analysis/budget를 freeze한다. LLM judge를 쓸 경우 model/prompt/rubric도 고정하고 reliability를 deterministic/human reference로 교정한다. 후보 generator와 judge가 같은 모델이라도 shared prompt/context로 답을 미리 넘기지 않는다. high-risk는 독립 human review를 요구한다. 개발자가 구현과 test를 같이 쓰더라도 보호된 regression 결과를 덮어쓸 수 없다.

model API alias가 provider에서 바뀌거나 tool version이 drift하면 실험을 `environment_drifted`로 표시한다. 비용 절감을 주장하면서 모델 버전도 바꿨다면 harness-only 효과가 아니라 composition 효과라고 보고한다. replay와 sandbox rerun의 결과를 같은 성공률 분모에 섞지 않는다.

## 6. Replay·shadow·canary

Replay는 CP state machine/selector/context assembly와 recorded inputs의 동작 비교에 적합하다. 모델을 다시 불러 출력을 생성하는 것은 rerun이며 stochastic하다. 과거 외부 write는 sandbox simulator receipt로 대체하고 실제 재호출하지 않는다. shadow는 side effect zero profile로 dispatch한다.

canary admission은 project opt-in, eligible low-risk classes, small bounded workload fraction/count, maximum spend, concurrency, abort signals, baseline fallback이 고정돼 있어야 한다. **fraction/count 값은 조직 정책에서 승인해야 하며 이 패키지는 임의의 운영 비율을 확정하지 않는다.** canary와 baseline이 같은 workspace에 동시에 write하지 않도록 별도 resources를 사용한다.

자동 abort: policy/security incident, unknown effect 발생, verifier tampering, persistent contract coverage drop, predetermined cost/reliability threshold 초과. 통계적으로 효과가 나빠도 사람이 기다리라고 할 수 있는 것과 무조건 abort할 safety event를 구분한다.

## 7. Promote 원자성

승인자는 exact candidate release digest, eval report digest, allowed target set, expiry를 bind한 PromotionGrant를 발급한다. ReleaseService가 현재 active release의 expected revision을 CAS하고 새 작업 admission pointer를 변경한다. 이미 실행 중인 Run은 원래 composition으로 끝내거나 명시적 checkpoint 전환을 따른다. running process의 prompt/pack 파일을 덮어써 바꾸지 않는다.

배포는 release set 단위이며 tool/pack/model/protocol 호환성을 함께 pin한다. mixed fleet은 capability negotiation으로 지원된 조합만 받는다. 부분 실패 시 기존 active version은 유지하거나 target별 quarantine하고 전체 성공으로 보고하지 않는다.

## 8. Rollback과 kill switch

rollback은 active composition pointer를 직전 검증 release로 돌리고 신규 admission을 차단한다. 완료된 외부 effect를 자동 취소하는 동작이 아니다. 활성 canary는 cancellation→effect reconciliation→resource cleanup을 거친다. schema/storage 호환성 문제가 생기면 db snapshot 복구·migration decision이 별도로 필요하다. release rollback과 data rollback을 같은 버튼으로 숨기지 않는다.

kill switch의 owner는 Authority/Operations이며 MetaProposer가 해제할 수 없다. source·config·package가 훼손된 경우 마지막 정상 release로 전환하되 revoked credential/승인이 되살아나지 않도록 최신 authority 상태를 사용한다.

## 9. Meta-loop 자신의 budget

root experiment budget 아래 후보 생성·judge·rerun·canary를 모두 계상한다. “평가를 더 하면 이길 수 있다”는 이유로 무제한 반복하지 않는다. 동일 hypothesis가 반복 실패하면 cooldown과 인간 검토를 요구한다. proposal 수/동시 experiments/holdout reuse cap은 정책으로 둔다. Harness evolution도 gate가 있는 bounded work다.

## 10. V3 완료 기준

최소 1개의 실제 안전한 개선 후보가 fake simulator가 아닌 V3 실행 pipeline을 통과하여 immutable experiment, baseline comparison, guarded canary, explicit promote, rollback drill 증거를 만들어야 한다. 효과가 없으면 **reject/inconclusive가 정확히 동작하는 것**도 필수 시험이다. 이번 ZIP은 이 기준의 설계이며 그 실험을 실행한 결과가 아니다.
