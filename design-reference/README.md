# AMPLAI V3 — Intent to Verified Work

**Design package 1.0 · 조사 기준 2026-09-15 · 설계만 / 구현·배포 없음**

> Resolve the goal. Compose the work. Verify the outcome. Improve the harness.

기존 `amplai-foundry.tar.gz`의 실제 구조와 최신 공식 자료를 바탕으로, V3 전체 목표를 설계했다. 단순 Graph framework 추가가 아니라 **자연어 의도→검증 가능한 계약→작업 그래프→적응형 실행→증거/검증→메타하네스 개선**을 잇는다. Foundry의 지식·승인 체계와 앱 독립성은 유지하고 불필요한 표면은 pack/adapter로 경량화한다.

## 먼저 열 파일

브라우저에서 [START_HERE.html](START_HERE.html)을 열면 인터넷 연결 없이 전체 설계를 읽을 수 있다. 외부 출처 링크는 사용자가 클릭할 때만 연결된다. 구현 에이전트는 [IMPLEMENTATION_START.md](IMPLEMENTATION_START.md)를 진입점으로 사용한다.

**읽는 순서:** [범위/선택](design/01_SCOPE_AND_DECISIONS.md) → [전체 구조](design/02_ARCHITECTURE.md) → [불변조건](design/03_INVARIANT_REGISTRY.md)·[Gate](design/04_GATE_MATRIX.md) → [기존 코드 감사](audit/BASELINE_AUDIT.md) → [구현 백로그](design/23_IMPLEMENTATION_BACKLOG.md).

## 포함 범위

| 구역 | 내용 |
|---|---|
| `design/` | 33개 주제별 상세 설계: 상태·트랜잭션·권한·driver·context·packs·QA·eval·meta·migration·운영 |
| `contracts/` | 30개 JSON Schema, 28개 invariant, 24개 gate, 25개 semantic validator, 상태 전이, API/command DTO, 논리 DB/port 규격 |
| `fixtures/` | 정상/오류/semantic-negative 합성 예시. 실제 승인·운영 설정 아님 |
| `implementation/` | 62개 의존 관계 포함 구현 작업, 35개 요구의 설계/schema/gate/test/task 추적표 |
| `eval/` | 112개 구현 인수/부정/장애 시험 명세 + property-test obligations. 런타임 실행 전 |
| `migration/` | 컴포넌트 변경 지도와 1,669개 normal file의 digest/보존·이관 분류. 삭제 실행 권한 없음 |
| `audit/` | 업로드 archive SHA-256, actual source anchors, 기준선 inventory와 이전 답변 정정 |
| `research/` | 공식 문서·표준·연구 36개 출처, 확인일·채택 이유·적용 한계 |
| `prompts/` | 다음 구현/리뷰/보안/visual/meta/migration 역할별 지시문 8개 |
| `release/` | **설계 패키지** 검수 결과, 한계, file checksum manifest |

## V3 설계에서 고정한 것

Core 사용자 명령은 `/work`,`/design`. Macro는 versioned WorkGraph DAG, node 내부는 bounded loop. 단일 활성 control plane + isolated workers + 로컬 SQLite/CAS를 기본으로 한다. 모델과 Driver는 분리하고 검증되지 않은 기능은 optional/disabled로 둔다. 승인·권한·실제 effect·현재 artifact 검증은 LLM의 문장 대신 코드/정책/증거로 강제한다.

Meta-Harness는 **Composition/Federation**과 **Evidence-driven Evolution**을 나눴다. 후보→독립 실험→shadow/canary→승인된 promote→rollback을 전부 설계했으며 자기 승인·평가 기준 약화·holdout 오염을 금지한다. 설계 전체를 한 번에 채택하되 실제 전환은 같은 V3 안에서 의존 순서·복구 검증을 거친다.

## 이전 검토에서 정정한 사항

handoff 파일은 optional marker일 수 있어 없음만으로 결함이라고 단정할 수 없다. `legacy_*`는 현재 governance가 참조하므로 대체 검증 전 삭제하면 안 된다. README Kit2.5.0 후보와 VERSION2.4.0은 release truth를 확인해야 한다. 실제 package는 `src/amplai_foundry/`를 유지한다. Astra는 모델이지 별도 Driver가 아니다.

## 정본과 해석

계약·불변조건·Gate·semantic rules·state machine은 `contracts/`가 정본이다. 설명은 `design/`. immutable ref/hash 규칙은 [33장](design/33_IDENTITY_REFERENCE_LIFECYCLE.md)을 반드시 읽는다. `FULL_DESIGN.md`와 HTML은 읽기용 투영이며 독립 편집 정본이 아니다. 충돌이 발견되면 구현 전에 DESIGN_CHANGE_PROPOSAL로 해결한다.

## 검수 범위

[DESIGN_REVIEW_REPORT.md](release/DESIGN_REVIEW_REPORT.md)와 [기계 판독 결과](release/DESIGN_VALIDATION_REPORT.json)에 이번 실제 검사와 미실행 항목을 구분했다. 스키마/문서/작업 DAG를 확인한 사실은 AMPLAI V3가 구현되어 동작한다는 의미가 아니다. 실제 모델 비용·사내 DB·RHEL·배포·crash test·meta 실험은 다음 구현에서 검증해야 한다.
