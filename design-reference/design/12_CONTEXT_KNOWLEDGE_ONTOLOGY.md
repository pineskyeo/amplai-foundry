# 12. Knowledge Runtime·Context·Ontology

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 현재 Foundry 모델을 유지한다

기존 8 memory kind(source/concept/principle/decision/question/architecture/experiment/map)와 Source→Proposal→Decision→Apply를 유지한다. stable knowledge/decision/work/evidence는 **검색·수명주기 view**이지 기존 객체를 네 개의 새 DB로 강제 이관하는 명분이 아니다. Decision의 승인 기록을 일반 메모리 요약으로 대체하지 않는다.

| view | 의미 | 정본 / 승격 |
|---|---|---|
| stable/domain | 승인된 용어·경계·불변조건·현재 동작 | Foundry canonical; governance를 거쳐 변경 |
| decision | 승인·거절·superseded·rationale | Authority와 canonical Decision의 링크 |
| work/session | 계획·시도·질문·중간 상태 | Runtime; 사실/권한 아님 |
| evidence/experience | 관측·verifier·실험·회귀 | CAS/RunRecord; 조건부 근거, 자동 규칙화 금지 |

## 2. Knowledge Readiness는 Goal 작성 이전부터 사용

현재 8개 readiness 항목을 유지한다: terminology, current behavior, boundary, invariants, SSOT, contradictions, acceptance, verifier. Goal Resolver는 각 항목을 `ready/missing/conflicting/not_applicable`과 source refs로 기록한다. 누락을 모두 '새 질문'으로 만들지 않고 registry→canonical→repo facts→evidence 순으로 탐색한다. acceptance 또는 authority에 영향을 주는 unresolved conflict는 실행을 막는다.

`not_applicable`에는 reason과 verifier rule이 필요하다. 언어모델 confidence 숫자 하나로 readiness를 통과시키지 않는다. “개발자가 보통 이렇게 한다”는 외부 지식이 이 프로젝트의 승인된 invariant를 덮어쓰지 못한다.

## 3. ContextBundle

bundle은 긴 복사본 하나가 아니라 **필수 core + 주소/버전/digest를 가진 map + 제한된 필요한 excerpts**다. 반드시 포함: 정확한 contract·graph node·authority bounds·현재 active decisions·app binding·invariants·acceptance/verifier plan. 추가 지식은 progressive disclosure한다. 핵심 보호 정보를 단순 pointer만 남겨 모델이 읽지 않고 작업하도록 허용하지 않는다. [R06,R17]

ref에는 scope/kind/id/revision/digest/trust/freshness/superseded_by/source timestamp/excerpt range가 붙는다. `ContextBundle` hash가 같아도 외부 URL의 현재 내용이 같다고 보지 않는다. 외부 자료는 ingest하여 snapshot ref로 고정하거나 unresolved external dependency로 표시한다. active invariant discovery의 completeness marker가 없으면 context 조립을 완료했다고 하지 않는다.

## 4. Context budget와 검색

고정 core 최소 크기, selected excerpt budget, reserve for tool results를 둔다. 오래된 summary만 읽고 원본 decision을 생략하지 않는다. 우선순위는 권한/보호 invariant > 현재 계약 > 현재 코드 사실 > 관련 canonical > 경험/외부 팁이다. user preference는 domain invariant가 아니며 적용 scope를 명확히 한다.

기본 resolver는 deterministic scope filters + registries + file/symbol/path 검색 + metadata joins다. lexical/semantic retrieval은 후보 탐색 플러그인이다. vector DB를 넣지 않는다는 것은 semantic search를 영구 금지한다는 뜻이 아니다. retrieval recall·staleness·privacy·maintenance가 baseline보다 좋아졌다는 eval 뒤에만 추가한다.

## 5. Ontology의 최소 kernel

DomainConcept, Relation, Constraint, Mapping을 지원하는 registry interface를 둔다. WorkGraph는 실행 관계이고 KnowledgeGraph는 의미·사실 관계이므로 저장 키와 API를 혼합하지 않는다. 반도체 DC의 장비/계측기/핀/물리 상태 owner·ABI 규칙은 `semiconductor-dc` pack이 공급한다. AMPLAI core는 PCMU/Cortex 전용 필드를 가지지 않는다.

RDF/TTL/SHACL 사용이 필요한 domain pack은 기존 canonical data와 source provenance를 보존한다. 안정 baseline은 W3C SHACL Recommendation이며 2026-08-28 SHACL 1.2는 Working Draft로 별도 experimental profile이다. Neo4j/graph DB는 필수 사항이 아니다. [R33,R34]

## 6. 발견→승격→폐기

worker discovery는 `Observation` 또는 기존 proposal intake로 보낸다. 증거 출처·대상 버전·관측 환경·재현 조건·반례를 보관하고 기존 지식과 중복/모순을 검사한다. 반복해서 관측됐다는 이유로 원칙이 자동 승격되지 않는다. Knowledge Governor가 기존 경로로 승인한다.

superseded 정보는 검색 기본 후보에서 낮추되 과거 Run 재현에는 읽을 수 있어야 한다. source 삭제와 supersede는 다르다. canonical change가 활성 run의 protected invariant를 바꾸면 별도 steering/revalidation event를 생성하고 하위 effect를 HOLD한다. 모든 외부 변경마다 불필요한 전체 replan을 하지 않고 affected refs dependency로 범위를 줄인다.

## 7. Hot/cold와 gardening

active/canonical definitions는 기본 context 표면에, raw evidence/history는 archive index에 둔다. `specs/012...` 대량 로그는 즉시 삭제하지 않고 referenced manifest→archive migration→link rewrite→backlink test→approved cleanup 순으로 이동한다. full repository gardening은 독립 Work이며 일반 구현 뒤의 gardening은 report-only다. secret-containing runtime state는 Git/export/LLM context에 유입하지 않는다.
