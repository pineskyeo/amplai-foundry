# 13. Skill·Capability Pack·App Binding

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 사용자 표면

일상 interface는 `/work`와 `/design` 두 개로 고정한다. Codex 등 host 문법은 adapter가 제공한다. `/work`는 의도를 검증된 결과까지 진행하며 `/design`은 설계 artifact까지만 생성한다. review/debug/research/plan/taskify/analyze/converge는 내부 capability다. 사용자에게 내부 stage 호출 순서를 외우게 하지 않는다.

SpecKit은 지우기 전에 pack으로 내린다. 동작이 중복된 grill-me/grilling은 design question policy와 비교해 필요한 questioning 규칙만 통합한다. eli12처럼 AMPLAI 실행 원리와 관계없는 표현 utility는 기본 설치에서 제외하되 사용자가 선택한 utility pack에 둘 수 있다. 과거 승인·결정 파일은 skill 정리와 함께 삭제하지 않는다.

## 2. Pack 단위

pack에는 ID/version/publisher/digest/signature/runtime protocol range/dependencies/entry capabilities/required permissions/model capability requirements/context refs/verifier profiles/eval cases/license/migration notes가 있다. schema는 `capability-pack.schema.json`. dependency cycle·version conflict·unknown publisher·elevated permission 요구는 설치 전 실패다. pack은 path 문자열만으로 실행 도구를 등록하지 못한다. tool adapter는 정책 registry에 존재해야 한다.

pack 내부 권장 구성은 `PACK.json`, `skills/`, `context/`, `verifiers/definitions/`, `eval/cases/`, `templates/`. 실행 파일을 포함하는 경우 코드 리뷰·sandbox allowlist·release attestation이 추가된다. remote instruction 다운로드로 install grant를 우회하지 않는다. [R17,R18,R36]

## 3. V3 기본 pack 경계

| pack | 역할 | core로 가져오지 않을 것 |
|---|---|---|
| spec | 요구·설계·task decomposition·critic rubrics | 모든 work에 긴 9단계 mandatory pipeline |
| software | build/test/lint/ABI/compatibility profiles | 특정 언어·repo 경로 하드코딩 |
| frontend | functional UX·accessibility·responsive·visual checks | 미감 점수만으로 자동 product approval |
| documents | structural/render/visual/readability evidence | Core에 DOCX/PDF renderer 의존성 |
| research | primary-source retrieval·date/provenance·uncertainty | 검색 결과를 canonical fact로 자동 승격 |
| ontology | semantic validation·mapping·contradictions | graph DB 강제 |
| semiconductor-dc | DC domain/context/safety/compatibility constraints | core에서 tester/production 직접 제어 |

이 설계에는 모두의 **interface와 validation profile**을 포함한다. 모든 domain pack의 실제 엔진 구현이 끝났다는 뜻이 아니다. V3 acceptance는 pack 설치/권한/호출/결과 수집 contract conformance와 명시된 대표 사례로 정의한다.

## 4. AppBinding

registry에 app_id, canonical repo identity, allowed roots, owner, environments, required invariants, target verifier profiles, data classification, write policy를 등록한다. 여러 이름(alias)이 같은 app을 가리킬 수 있으나 대상 registry ID는 하나다. repo URL·경로는 입력에서 직접 실행 cwd가 되지 않는다. 표준화된 repo identity + signed/local authorized binding으로 resolve한다.

앱 프로젝트는 kit uninstall 이후에도 build/test/domain data를 사용할 수 있어야 한다. `.ai-team`는 개발도구 구성, `.amplai`는 임시/영속 실행상태(권한 분리), `.agents`는 generated skill surface다. 앱 source가 이 경로를 import하거나 production binary가 AMPLAI control plane에 의존하면 architecture gate 실패다.

## 5. 설치·갱신·해제

Foundry distribution이 signed ReleaseSet으로 pack+kit+protocol compatibility를 고정한다. installer는 owned paths만 변경하며 `expected_old_digest`가 달라진 파일은 conflict report 후 중지한다. app-local override는 별도 namespace에 두고 upstream 파일에 섞지 않는다. required:false marker가 없다는 이유만으로 corruption이라 판정하지 않는다.

삭제는 `owned=true`, current digest matches, no protected references, verified replacement/retirement decision, backup/rollback available일 때만 실행 가능하다. symlink/mirror는 host마다 확인한다. installation receipt에는 실제 설치 digest와 generated surface inventory를 남긴다. 자동 plugin install 또는 자동 permission escalation은 금지한다.
