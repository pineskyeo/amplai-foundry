# 14. Verifier·Evidence·문서/UI 품질

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 완료의 의미

worker exit0, 모델의 '완료', PR 존재, screenshot 존재, evaluator의 자연어 칭찬은 각각 Done이 아니다. **해당 계약 revision의 모든 mandatory acceptance에 대해 고정된 평가 방식으로 얻은 Evidence와 Verdict가 있어야 한다.** 결과는 `pass/fail/inconclusive/waived`; waiver는 authorized human decision이고 pass와 구분한다. 보안·authority 불변조건은 waived 불가다.

`VerificationPlan`은 acceptance_id → verifier_id@version → subject artifact selector → required evidence → environment → threshold → independent reviewer requirement를 연결한다. threshold를 implementation 후 맞춰 낮추지 않는다. plan 변경은 계약 변경이다. [R10]

## 2. 증거와 신뢰

Evidence에는 producer, actual artifact digest, tool/version/environment, started/finished, exit code, observation/result refs, subject contract/graph revision, redaction 상태를 넣는다. artifact digest는 agent가 텍스트로 선언하는 값만 믿지 않고 ArtifactService가 재계산한다. VerifierService의 attestation 또는 authenticated server-side execution receipt가 판정의 출처를 증명한다. 서명 자체는 내용의 의미적 정확성을 보증하지 않는다.

code verifier는 change diff뿐 아니라 intended build tree의 정확한 revision을 실행한다. tool command는 VerifierProfile allowlist에서 선택하고 worker가 `/bin/true`를 테스트로 바꾸지 못한다. test changes는 product code changes와 함께 review하되 기존 protected regression corpus는 별도 신뢰 경계에 둔다. 테스트를 삭제해서 green을 만든 경우 coverage/diff guard가 잡는다.

## 3. Verification 층

| 층 | 확인 | 불가능/부족한 것 |
|---|---|---|
| schema/semantic | 구조·참조·범위·타입·불변조건 | 실제 제품 동작 증명 아님 |
| deterministic | unit/integration/build/lint/ABI/negative tests | UX/미감/도메인 만족 전체 증명 아님 |
| artifact inspection | 실제 rendered/file/system output | screenshot만으로 기능 성공 아님 |
| model-assisted critique | 누락·명료성·일관성·루브릭 평가 | 독립된 객관 truth 아님 |
| human acceptance | 중요 의도·취향·배포 승인 | 자동 안전 gate를 무시할 권한은 없음 |

모든 Work에 모든 층을 실행하지 않는다. risk/asset type에 따라 profile을 선택하되 mandatory invariant는 공통이다. Planner→Builder→Evaluator는 uncertainty가 높은 일에 쓰며 작은 deterministic 수정에 세 agent를 강제하지 않는다. [R08]

## 4. Frontend pack 상세

입력은 사용자 흐름, reference direction, design tokens, target devices, accessibility target, non-goals, example content다. 단계는 reference/constraints 확인→작은 visual direction 초안→구조/interaction 구현→실제 브라우저 render→functional+accessibility+responsive 확인→visual critique→bounded repair다.

필수 evidence: 대표 page의 실제 screenshot(정해진 viewport), interaction trace, console/network errors, overflow/clipping/overlap report, keyboard flow, empty/loading/error state, text density/readability 관측. golden baseline은 사람이 승인한 revision이며 agent가 baseline을 갱신하여 visual diff를 없애면 실패다. Playwright screenshot 비교는 재현 환경의 변화 탐지이며 **'아름답다'를 증명하지 않는다**. font/browser/OS/animation/timestamp를 고정한다. [R35]

subjective acceptance는 예: “운영자가 lot 상태·다음 조치를 1화면에서 식별할 수 있다”처럼 task-based rubric으로 쓴다. 단일 aesthetics 9/10을 요구하지 않는다. 사람이 최소한 direction과 최종 중요 UX를 확인하도록 optional human gate를 바인딩한다. visual judge가 fail을 발견하면 좌표/영역/대상 요소/재현 viewport/이유를 구조화한다.

## 5. Documents pack 상세

문서 타입별 renderer를 선택하고 exact version/font availability를 검사한다. DOCX/PPTX/PDF/SVG/HTML 원본뿐 아니라 **최종 렌더링 결과**를 검수한다. 구조 검사(페이지·슬라이드 수, 제목 계층, 테이블 셀, 링크)와 시각 검사(글리프 누락, clipping, 겹침, 읽기 순서, 여백, 대비)를 나눈다. 모든 페이지 thumbnail→flagged page full render→중요 페이지 상세 확인 경로를 둔다.

SVG는 viewBox/size/font/text anchors/clip path/filter/external asset을 검사한다. embedding target별 지원 차이를 고려해 필요 시 PNG fallback을 쓰되 의미 정보와 원본을 보존한다. 폰트가 없으면 임의 대체 후 success하지 않고 지정 대체 정책 또는 HOLD다. font 파일의 무단 배포를 금지한다. raster fallback은 해상도·선명도·접근성 비용을 기록한다.

golden document ingestion은 license/source provenance 확인→content와 style 분리→sanitized style tokens/layout examples→human-approved baseline 순서다. 개인/회사 민감문서를 외부 모델로 자동 보내지 않는다. 사용자에게 보일 글꼴·글자·이미지 깨짐을 reproducible acceptance로 만든다.

## 6. Repair와 convergence

repair는 failure finding IDs를 입력으로 받아 수정한 artifact와 해결 evidence를 제출한다. 동일 failure signature가 반복되거나 수정이 다른 mandatory acceptance를 깨면 bounded budget 내 재시도 후 HOLD한다. pass에 가까워 보인다는 모델 판단만으로 반복 수를 무한 연장하지 않는다.

최종 global verifier는 node별 pass뿐 아니라 integration acceptance·cross-app compatibility·doc freshness·pending effects·open human questions를 확인한다. 서로 다른 branch에서 각각 pass한 결과가 합쳐진 뒤도 pass인지 별도 검사한다. 검증 중 code/contract가 바뀌면 그 verdict는 active revision에 사용할 수 없다.

## 7. 기존 doctrine 보존

Doc freshness와 gardening을 통합하면서 `repository_gardening` enum drift를 고친다. 일반 작업 후 incremental gardening은 report-only; 대량 삭제/이동/format은 scope가 선언된 독립 Work로 제한한다. 테스트가 아직 구현되지 않은 상태는 `not_run`으로 표시하며 '명세 있음'을 '검증 완료'로 집계하지 않는다.

## 8. mandatory와 waiver의 정확한 종료 규칙

`goal.verified`는 현재 계약의 모든 mandatory acceptance가 trusted PASS일 때만 가능하다. optional acceptance의 승인된 waiver는 별도로 기록한다. mandatory criterion을 면제해야 하는 합법적 변경은 승인된 새 contract revision에서 scope/acceptance를 바꾸고 재검증한다. waived verdict를 PASS로 세거나 protected invariant를 면제하지 않는다.
