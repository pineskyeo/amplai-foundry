## Goal

Codex CLI run의 `turn.completed` usage와 그 run에 고정된 model profile만을 출발점으로 비용을 산출할 수 있는 필요충분조건을 정한다. 조건을 모두 충족하면 비용을 `cost_microunits`, `currency: USD`, `status: estimated`, 감사 가능한 non-null `source_ref`로 기록하고, 하나라도 충족하지 않으면 비용은 `unknown`으로 유지한다. 이 정책은 Claude CLI의 provider-reported 비용 정규화, D-084 budget 정산, Observatory의 measured/estimated/unknown 구분과 의미적으로 일치해야 한다.

비목표는 Codex CLI나 공급자 API 변경, 현행 가격값의 채택, 기존 run backfill 또는 자동 reconciliation, 추정치를 measured/공급자 확정 비용으로 승격, D-084의 reservation 보존 및 estimate 초과 비차단 원칙 변경이다. 후속 구현의 변경 범위도 `specs/design/codex-cli-cost-estimation/design.md` 하나로 제한한다는 요청은 실행 가능한 제품 변경과 양립하지 않으므로, 이 문서에서는 그것을 이번 설계 산출물의 범위 제한으로 해석한다. 실제 구현 작업은 아래 계획에 열거한 코드·계약·테스트 변경을 별도 승인된 work goal에서 수행해야 한다.

## Current State

`EventNormalizer`의 초기 usage는 두 token 합계와 비용을 모두 `None`, 통화를 USD, 상태를 `unknown`, `source_ref`를 `None`으로 둔다 (`src/amplai_foundry/agent_drivers/protocol.py:93`). Codex `turn.completed`는 완료 표시만 하고, 공통 usage 정규화는 nonnegative integer인 `input_tokens`와 `output_tokens`만 복사해 상태를 `measured`로 바꾼다; cached input, cache-write input, reasoning output category는 보존하지 않는다 (`src/amplai_foundry/agent_drivers/protocol.py:120`, `src/amplai_foundry/agent_drivers/protocol.py:150`). 실제 qualification artifact의 Codex usage에는 `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`가 함께 나타난다 (`specs/018-v3-real-execution/artifacts/codex-budget_accounting-usage.bin:1`). 그러므로 현재 정규화된 두 합계만으로는 서로 다른 단가가 적용될 수 있는 category를 재구성할 수 없고, token 수만으로 모델이나 가격 tier를 추측할 수도 없다.

Claude에 한해서는 성공 `result`의 유효한 `total_cost_usd`를 1 USD당 1,000,000 microunits로 반올림해 `estimated`로 기록하고, `source_ref`에 고정 ID/revision 및 원 event digest를 넣는다 (`src/amplai_foundry/agent_drivers/protocol.py:160`, `src/amplai_foundry/agent_drivers/protocol.py:175`). Codex event에 같은 필드가 있어도 이 경로는 적용되지 않는다는 계약 테스트가 있다 (`tests/v3/test_rc02_driver.py:237`). 즉 Claude 값은 공급자가 event에서 보고한 estimate의 정규화이고, 제안하는 Codex 값은 별도 가격 snapshot으로 계산한 estimate라서 상태는 같아도 provenance 종류가 다르다.

run record는 goal에 고정된 profile refs를 복사하며 usage를 `unknown`으로 시작하므로 실행과 model profile의 결합점은 이미 존재한다 (`src/amplai_foundry/runtime/execution/service.py:532`). model-profile schema는 `price_snapshot_ref`를 required하지만 nullable인 ref로 정의한다 (`src/amplai_foundry/runtime/contracts/data/schemas/model-profile.schema.json:62`, `src/amplai_foundry/runtime/contracts/data/schemas/model-profile.schema.json:79`). 현재 Codex 설치 경로는 `provider_model_id`를 기록하면서 `price_snapshot_ref`를 `None`으로 생성하므로 가격 소스가 아직 바인딩되지 않는다 (`src/amplai_foundry/runtime/execution/codex.py:299`).

BudgetService는 `estimated`이면서 input/output token이 있는 usage의 token 합계를 보고값으로 정산하되, reservation의 비용은 유지하고, 추정 비용이 예약을 넘으면 `budget.estimate_over`만 기록한다 (`src/amplai_foundry/runtime/budgets/service.py:94`, `src/amplai_foundry/runtime/budgets/service.py:103`, `src/amplai_foundry/runtime/budgets/service.py:117`). D-084도 같은 정책과 token 없는 estimate의 `unknown` fallback을 확정한다 (`docs/workstreams/v3-real-execution/DECISIONS.md:295`). 따라서 현재 Codex 정규화 결과는 token은 보고값으로 정산될 수 있지만 비용은 없는 상태이고, 비용 reservation은 해제되거나 0으로 치환되지 않는다.

Observatory는 통화별로 measured, estimated, unknown run과 금액을 분리하고, estimated 또는 unknown이 하나라도 있으면 exact `total_cost_microunits`를 `None`으로 둔다 (`src/amplai_foundry/evaluation/observatory.py:163`, `src/amplai_foundry/evaluation/observatory.py:176`, `src/amplai_foundry/evaluation/observatory.py:182`). 따라서 Codex estimate를 추가해도 exact total의 의미는 바뀌지 않으며, 현행 unknown run 일부가 estimated bucket으로 이동할 뿐이다.

## Options

1. **조건부 pinned rate-card 추정.** run-bound model profile이 immutable price snapshot을 가리키고 event가 그 snapshot의 모든 과금 category를 제공할 때만 계산한다. 장점은 Codex 비용의 운영 가시성과 budget estimate-over 관측이 생기고, revision/digest 및 계산 provenance로 재현·감사가 가능하다는 점이다. 단점은 공급자 가격 변경과 category 의미 변경의 drift를 snapshot 수명주기와 qualification으로 관리해야 하며, provider invoice와 차이가 날 수 있다는 점이다.

2. **공급자 확정 비용만 허용.** provider가 run별 확정 비용을 직접 제공할 때만 비용을 기록한다. 정확성 주장은 가장 강하고 rate-card drift가 없지만 Codex stream에 그러한 값이 없으면 계속 unknown이며, 현재 objective의 token 기반 운영 가시성을 제공하지 못한다. 추후 확정값이 생기더라도 별도 provenance와 reconciliation 정책 없이는 기존 estimate를 자동 대체해서는 안 된다.

3. **계속 unknown 유지.** 구현과 가격표 유지 부담이 가장 작고 잘못된 정밀도를 피하지만, 비용 기반 관측과 용량 계획이 불가능하고 reservation 추정 초과를 발견할 수 없다. 감사 관점에서는 “모름”의 원인은 명확하지만 계산 가능한 run조차 활용하지 않는다.

세 선택지 모두 unknown을 0으로 보거나 reservation을 해제해서는 안 된다. 1번만 운영성과 감사 가능성을 함께 개선하며, 2번은 정확성 우선의 장기 경로, 3번은 조건 미충족 시의 안전한 fallback으로 남긴다.

## Decision

**옵션 1, 조건부 pinned rate-card 추정을 채택한다.** 단, 아래 gate를 모두 만족한 단일 `turn.completed`에만 적용한다.

- run record의 `model_profile_ref`가 실행 시 고정된 profile을 해석하고, profile의 provider 및 exact `provider_model_id`가 Codex 실행 binding과 일치해야 한다. stream token 수로 모델을 추측하거나 mutable alias를 해석하지 않는다.
- profile의 non-null `price_snapshot_ref`가 immutable snapshot revision을 해석해야 한다. snapshot 소유 경계는 model profile 바깥의 versioned contract/registry object이고 profile은 ref만 소유한다. snapshot에는 공급자 원문 URL 또는 문서 식별자, 확인일, effective date 또는 적용 기간, 공급자와 exact model ID, 각 token category와 적용 단위(예: USD per N tokens), `currency: USD`, immutable revision, canonical content digest, 공급자 문서/API release pin이 필수다. 확인일이나 mutable alias만으로는 pin으로 인정하지 않는다.
- snapshot은 category가 총합에 포함되는 방식을 명시해야 한다. 최소한 Codex 관측 category인 input, cached input, cache-write input, output, reasoning output 각각을 별도 rate 또는 명시적인 “다른 category에 포함되어 별도 과금 없음” 규칙으로 완전히 매핑해야 한다. `input_tokens`가 cached input을 포함하는지 같은 중첩 의미도 snapshot revision에 고정한다. 필요한 category가 누락됐거나 음수·boolean·비정수·모순된 합계면 추정하지 않는다.
- 계산은 category별 billable token과 snapshot의 정확한 rational rate를 곱해 USD 금액을 합한 뒤 마지막에 한 번만 1 USD당 1,000,000 microunits로 round-half-to-even 한다. 중간 category별 반올림은 하지 않는다. 결과가 nonnegative safe integer 계약 범위를 넘거나 유한·정확한 rational 계산으로 표현되지 않으면 `unknown`이다.
- 성공 결과는 기존 usage의 provider-reported `input_tokens`와 `output_tokens`를 유지하면서 `cost_microunits: <nonnegative integer>`, `currency: USD`, `status: estimated`로 기록한다. `source_ref`는 non-null이며 최소 `{id, revision, digest}` 호환 식별자와 함께 run-bound model-profile ref/digest, price-snapshot ref/revision/digest, `turn.completed` event digest, category token 입력, 계산 규칙 revision을 해석할 수 있어야 한다. raw private payload는 보존하지 않는다.
- binding 없음, null/unresolvable snapshot, digest/revision/release pin 실패, provider/model/currency/unit 불일치, 지원하지 않는 모델·tier, 불완전하거나 잘못된 category, overflow 중 하나라도 있으면 비용 필드는 `None`, 비용 상태는 `unknown`, `source_ref`는 `None`으로 유지한다. 부분 가격을 합산하거나 0을 대입하지 않는다. token 관측 자체는 budget 정산을 위해 별도로 보존할 수 있지만 “token measured + cost unknown”을 표현할 수 있도록 usage status 계약을 분리하거나 명시적으로 확장해야 하며, 비용을 알 수 없는데 전체 status를 `measured`로 남기는 현 상태는 후속 계약 변경에서 제거한다.

Claude CLI의 `total_cost_usd`와 Codex 계산값은 모두 `estimated`다. Claude `source_ref`는 provider result event와 해당 정규화 revision을, Codex `source_ref`는 provider usage event와 run-bound model profile, pinned price snapshot, 계산 revision을 식별한다. 어느 쪽도 `measured` 또는 invoice-confirmed로 승격하지 않는다.

D-084를 그대로 보존한다. 두 estimate 경로 모두 input/output token이 있으면 token을 보고값으로 정산하고 token 초과는 기존 overrun 규칙을 따른다. 추정 비용은 usage에만 남고 reservation 비용을 대체하거나 해제하지 않으며, 비용 estimate가 reservation을 넘으면 `budget.estimate_over`를 관측하되 그 사실만으로 work를 차단하지 않는다. estimate 조건이 실패한 unknown 비용도 0이 아니며 reservation을 유지한다.

Observatory도 현행 의미를 보존한다. measured, estimated, unknown 비용을 통화별로 분리하며, estimated 또는 unknown run이 포함되면 exact total을 제시하지 않는다. Codex 추정치는 known estimate sum에는 포함될 수 있지만 actual/measured total에는 포함될 수 없다.

## Risks

- 공급자 문서가 수정되거나 alias가 다른 모델을 가리키면 과거 계산이 재현되지 않을 수 있다. immutable revision, digest, release pin과 model-profile binding을 모두 요구하고 mismatch는 unknown으로 닫는다.
- token category의 포함 관계를 잘못 해석하면 cached 또는 reasoning token을 이중 과금할 수 있다. snapshot이 category semantics와 formula를 명시하고 fixture 기반 계약 테스트를 통과하기 전에는 모델을 지원하지 않는다.
- rate tier가 context size, batch, region, 계정 계약 또는 날짜에 따라 달라질 수 있다. 실행 binding만으로 유일한 tier를 선택할 수 없으면 unknown이며 가장 그럴듯한 tier를 선택하지 않는다.
- microunit 반올림 때문에 공급자 invoice와 미세한 차이가 생길 수 있다. 최종 합계에서 한 번의 half-even 반올림을 고정하고 `estimated`를 유지한다.
- `status` 하나가 token 정확성과 cost 정확성을 동시에 나타내 현재의 `measured` token/unknown cost 상태를 모호하게 만든다. 후속 계약은 축을 분리하거나 호환 가능한 명시 상태를 정의해야 하며, Observatory와 BudgetService를 함께 바꾸지 않으면 회귀 위험이 있다.
- snapshot 갱신이 기존 profile을 조용히 바꾸면 동일 run의 결과가 달라진다. 기존 object mutation을 금지하고 새 revision/digest 및 새 model-profile binding으로만 갱신한다. 기존 run backfill이나 자동 reconciliation은 하지 않는다.
- 외부 가격 원문을 저장소가 신뢰 가능한 사실로 오인할 수 있다. snapshot은 provenance를 가진 운영 계약이지 provider invoice가 아니며, 사람 또는 승인된 공급 절차가 검토해야 한다.

## Implementation Plan

1. usage 계약을 먼저 개정해 provider raw category와 token 관측 상태를 비용 상태와 손실 없이 표현하고, 기존 consumer 호환 규칙 및 unknown fallback을 명시한다. `turn.completed`의 input/cached/cache-write/output/reasoning category 정상·누락·잘못된 타입·중첩 불일치 protocol 계약 테스트를 추가한다.
2. immutable price-snapshot schema와 registry ownership을 정의한다. source URL/document ID, verified date, effective date, provider, exact model ID, category semantics/rates, unit, USD currency, revision, digest, release pin을 required로 만들고 mutation, alias-only/date-only pin, digest mismatch, unsupported currency/unit/model을 거부하는 schema/registry 테스트를 추가한다.
3. Codex profile provisioning이 승인된 snapshot ref를 명시적으로 받도록 설계하고 run-bound `model_profile_ref`에서만 해석한다. null ref는 계속 허용하되 비용은 unknown이다. profile/provider/model/snapshot mismatch와 unresolvable ref 테스트를 추가한다.
4. protocol 또는 별도 pure estimator 경계에 exact rational category 계산을 구현한다. final-only half-even rounding, cached/non-cached 분리, reasoning 포함 규칙, zero usage, safe-integer 경계, overflow를 테스트한다. 현행 가격값은 이 작업에서 추가하지 않고 승인된 fixture snapshot만 사용한다.
5. `source_ref` provenance contract를 확장해 event, model profile, snapshot, formula revision과 계산 입력을 digest로 재현하게 한다. Claude provider-event provenance와 Codex rate-card provenance가 모두 non-null이고 서로 다른 kind를 갖는 계약 테스트, private payload 비보존 테스트를 추가한다.
6. BudgetService 회귀 테스트로 estimated token 정산, reservation 비용 보존, token overrun, cost-only/missing-token unknown, `budget.estimate_over` 발생 및 비차단, unknown에서 reservation 미해제를 검증한다. D-084의 의미는 수정하지 않는다.
7. Observatory 계약 테스트로 Codex estimate의 estimated bucket 편입, measured/estimated/unknown 분리, estimated 또는 unknown 존재 시 exact total `None`, 통화 불일치 비합산을 검증한다.
8. end-to-end fixture로 (a) 완전하고 일치하는 binding의 estimated 결과, (b) category 누락, (c) snapshot 없음, (d) model mismatch, (e) digest/release drift, (f) unsupported model/tier, (g) overflow의 unknown 결과를 검증한다. `python -m pytest`, Ruff, mypy, knowledge lint와 repository documentation validation을 실행하고 실패 명령·exit code를 숨기지 않는다.
9. 별도 승인 없이는 기존 run을 backfill/reconcile하지 않고, provider 확정 비용 경로가 생기면 새 설계에서 estimate 대체·차이 기록·권한 경계를 결정한다.

## Sources

- Usage 초기값과 Codex/Claude 정규화: `src/amplai_foundry/agent_drivers/protocol.py:93`, `src/amplai_foundry/agent_drivers/protocol.py:120`, `src/amplai_foundry/agent_drivers/protocol.py:150`, `src/amplai_foundry/agent_drivers/protocol.py:160`, `src/amplai_foundry/agent_drivers/protocol.py:175`
- 실제 Codex category 표본: `specs/018-v3-real-execution/artifacts/codex-budget_accounting-usage.bin:1`
- run/profile binding과 초기 unknown usage: `src/amplai_foundry/runtime/execution/service.py:532`
- model profile의 price ref 계약과 현행 Codex profile: `src/amplai_foundry/runtime/contracts/data/schemas/model-profile.schema.json:62`, `src/amplai_foundry/runtime/execution/codex.py:299`
- BudgetService 정산과 집계: `src/amplai_foundry/runtime/budgets/service.py:94`, `src/amplai_foundry/runtime/budgets/service.py:117`, `src/amplai_foundry/runtime/budgets/service.py:133`
- D-084 결정: `docs/workstreams/v3-real-execution/DECISIONS.md:295`
- Observatory 비용 상태 집계: `src/amplai_foundry/evaluation/observatory.py:163`, `src/amplai_foundry/evaluation/observatory.py:182`
- 기존 계약 테스트: `tests/v3/test_rc02_driver.py:163`, `tests/v3/test_rc02_runtime.py:433`, `tests/v3/test_dev03_observatory.py:57`
