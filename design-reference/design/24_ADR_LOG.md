# 24. Architecture Decision Records

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


다음 결정은 이번 설계안의 선택이다. 공식 문서가 이 모든 선택을 명령한다고 해석하지 않는다. 실제 deployment constraint가 다르면 ADR revision으로 근거·영향·회귀·승인 범위를 명시한다.

| ADR | 결정과 이유 | 검토 후 제외한 대안 / 비용 |
|---|---|---|
| ADR-01 | Intent→Verified Work를 V3 목표로 정의 | Graph가 Loop의 세대교체라는 branding 제외; topology보다 목표/검증이 우선 |
| ADR-02 | 기존 `amplai_foundry` package와 governance 유지 | 전면 rename은 호환성 비용만 늘림; runtime submodules는 새로 구성 |
| ADR-03 | modular monolith CP + isolated workers | microservices/K8s/Temporal 강제 제외; 추후 요구와 conformance 시 확장 |
| ADR-04 | Macro DAG, micro bounded loop | arbitrary cyclic macro graph는 종료/복구 복잡성; 탐색은 node loop/새 graph revision |
| ADR-05 | frozen contract+typed artifact-bound verification | free-text goal만으로 completion 판단 제외 |
| ADR-06 | hard invariant gates + adaptive strategies | 모든 작업에 긴 pipeline 강제 제외; tiny도 안전 검증은 유지 |
| ADR-07 | user hint optional, verified registry target mandatory | target_app_hint requirement 삭제를 target auth 삭제로 오해하지 않음 |
| ADR-08 | single active CP/local SQLite + outbox/inbox | NAS SQLite 또는 crossDB exactly-once 주장 제외; 단일 노드 한계 공개 |
| ADR-09 | authority ledger와 canonical content/runtime truth 분리 | Git만으로 live revocation/lease 관리하거나 SQLite가 canonical memory 정책을 몰래 대체하지 않음 |
| ADR-10 | capability intersection + brokered effects | prompt-only 권한·무조건 승인 UI 의존 제외 |
| ADR-11 | one Run per scheduled Work attempt | 실패 이력을 덮어쓰거나 재시도를 완료로 감추지 않음 |
| ADR-12 | native async/steering 사용하되 durable ledger 유지 | provider ACK를 applied/cancelled로 오해하지 않음 |
| ADR-13 | model과 driver 분리 | AstraDriver 제거; 모델 교체가 transport 재구현이 되지 않게 함 |
| ADR-14 | qualified CLI baseline, experimental app-server opt-in | 현재 문서 경고가 있는 API를 production 필수로 pin하지 않음 [R22] |
| ADR-15 | two public commands + signed capability packs | public skill proliferation/monolithic document renderer core 제외 |
| ADR-16 | progressive context + mandatory governing core | 모든 문서 prompt dump 또는 pointer만 주고 필수 규칙 누락 제외 [R06] |
| ADR-17 | ontology port와 기존 8 kinds 유지 | graph DB/vector DB 의무화·네 가지 memory DB 강제 이관 제외 |
| ADR-18 | observed action/usage/evidence trace, no private CoT | 비용 큰 원시 대화 무제한 저장 및 민감 reasoning 수집 제외 |
| ADR-19 | Observatory first, Meta evolution gated | 개선을 측정할 baseline 없이 자동 자기변경 활성화 제외 |
| ADR-20 | Federation와 Evolution을 별도 service로 | 만능 supervisor agent가 권한·평가·배포 모두 수행하는 구조 제외 |
| ADR-21 | independent eval/holdout + explicit inconclusive | 임의 점수/표본수로 자동 promote, benchmark leakage 제외 |
| ADR-22 | render-based docs/UI QA + functional/human rubric | source 문법 통과나 screenshot diff만으로 제품 미감/UX 보증 제외 |
| ADR-23 | class C protected changes 별도 governed work | evaluator/authority를 후보가 바꾸어 자기 개선을 증명하는 구조 제외 |
| ADR-24 | one full V3 target, staged migration within it | big-bang overwrite 금지; 단계가 다음 세대로의 미루기는 아님 |
| ADR-25 | preserve legacy guards until proven replacement | 파일명 legacy만으로 삭제 금지; 코드가 많아도 안전 의미 우선 |
| ADR-26 | pack/kit release pin과 non-destructive installer | latest 자동 갱신·사용자 override overwrite 제외 |
| ADR-27 | postfact attestation reverse refs, immutable reference DAG | contract↔plan/qualification hash cycles를 만들지 않음 |
| ADR-28 | provider/cloud use governed by data classification | on-prem 실패 시 unapproved cloud fallback 금지 |

재검토 trigger는 observed bottleneck, unsupported target requirement, actual incident, primary-source status change, audited cost/quality result다. 유행어 변경 자체는 architecture rewrite trigger가 아니다.
