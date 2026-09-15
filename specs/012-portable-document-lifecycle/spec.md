# Feature Specification: Portable Document Lifecycle

**Feature Branch**: existing `main`; no branch, commit, remote, or publication created

**Created**: 2026-09-11

**Status**: Approved local implementation — W003 RUNNING under E021; S01-S07 locally verified, S08 integrated canaries and completion review in progress

**Input**: Synapse split의 여러 개발자가 같은 검증 절차와 버전별 문서를 사용하도록
공용 Kit를 보완한다. Astra 설계의 요구를 기존 Foundry에 맞게 구현하고 회귀 없이
배포 준비 근거와 HTML 보고를 제공한다. Native Work는 `CR-SYNAPSE-APP-SPLIT-W003`다.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Start and update a real development workflow (Priority: P1)

개발자는 새 저장소에서도 기존 `work/design` 진입점을 사용한다. Kit 설치만 성공하고
실제 검증을 실행하지 못하는 상태는 설치 완료가 아니다. 기존 저장소의 도메인 규칙,
인증·신뢰 설정, 개발자 수정, 공용 Project Store 연결은 보존한다.

**Why this priority**: 분리한 앱마다 다른 수작업 절차가 생기면 병렬 개발의 안전성이 없다.

**Independent Test**: 빈 저장소 canary와 기존 로컬 수정 canary에서 설치, 실제 작업
계약 검증, 갱신, 재설치, 제거 및 중단 복구를 실행한다. 앱별 비소유 파일을 비교한다.

**Acceptance Scenarios**:

1. **Given** 필요한 기본 흐름이 없는 새 저장소, **When** 명시적으로 baseline과 확장을
   설치하면, **Then** 두 host의 진입점과 실제 계약·준비도·검증 명령이 작동한다.
2. **Given** 소유 파일의 로컬 수정이나 같은 버전의 다른 내용, **When** 갱신하면,
   **Then** 충돌을 보고하고 부분 설치·자동 덮어쓰기 없이 원본을 보존한다.
3. **Given** 설치 도중 오류, **When** 복구하면, **Then** 해당 설치가 만든 변경만
   되돌리고 기존 앱 규칙, host 신뢰 설정, Store와 사용자 파일은 그대로 둔다.

### User Story 2 - Review documentation against the actual change (Priority: P1)

개발자는 기능을 바꾸면 문서 영향을 같은 작업 흐름에서 확인한다. 검토는 파일 날짜가
아니라 문서와 의존 대상의 실제 내용에 묶인다. 문서 내용이 그대로여도 영향이 없다는
설명과 최신 근거를 남길 수 있다. 전체 검사가 불가능하면 그 범위를 표시하고 차단한다.

**Why this priority**: 여러 앱의 코드와 문서가 서로 다른 시점을 설명하면 회귀를 찾기 어렵다.

**Independent Test**: 대량 변경, rename/delete, 문서만의 계약 변경, 온톨로지 pin 변경,
읽기 오류와 오래된 검토를 넣어 영향 목록과 검토 상태를 확인한다.

**Acceptance Scenarios**:

1. **Given** 40개를 넘는 변경과 삭제된 필수 문서, **When** 영향 검사를 하면,
   **Then** 전체 변경과 삭제를 반영하며 조용한 목록 절단이나 영향 0건 PASS가 없다.
2. **Given** 문서 내용은 같지만 의존 버전이 달라짐, **When** 새 근거·이유·검토자를
   기록하면, **Then** 정확히 그 내용에만 검토 완료가 적용된다.
3. **Given** 검토 뒤 코드·문서·의존 pin이 변경됨, **When** 완료를 요청하면,
   **Then** 이전 검토는 재사용되지 않는다. 공백·날짜 변경만으로 검토를 대신하지 않는다.

### User Story 3 - Read the right version without exposing private material (Priority: P1)

사용자, 개발자, 운영자와 책임자는 같은 소유 문서에서 만든 목적별 HTML을 읽는다.
허용된 공개 범위와 명시적인 release set에 맞는 문서만 검색·목록·본문에 포함한다.
역사 문서는 명시적으로 선택할 수 있지만 현행 정본으로 섞이지 않는다.

**Why this priority**: 외부에 숨겨야 할 제목 하나나 검증되지 않은 최신 안내도 잘못된 전달이다.

**Independent Test**: 같은 topic의 여러 버전, stale 정본, superseded·미분류 문서,
private canary, script가 든 제목·본문을 넣고 오프라인 결과를 검사한다.

**Acceptance Scenarios**:

1. **Given** 같은 공개 범위·topic·release에 active 정본 두 개, **When** 목록을 만들면,
   **Then** 충돌로 차단한다. 다른 release의 역사 자료는 별도로 공존할 수 있다.
2. **Given** 내부 자료와 외부 승인 자료가 섞임, **When** 외부용 묶음을 만들면,
   **Then** 제목·검색어·경로·숨김 metadata를 포함해 내부 자료가 결과에 없다.
3. **Given** 네트워크가 없고 스크립트를 끔, **When** HTML을 열면,
   **Then** 본문·탐색·출처·버전이 읽히며 키보드, 작은 화면과 인쇄를 지원한다.
4. **Given** 검증되지 않은 새 commit, **When** 최신 안내를 요청하면,
   **Then** 검증된 release set을 임의로 교체하지 않는다.

### User Story 4 - Preserve history and recover safely (Priority: P2)

관리자는 낡은 문서를 무조건 삭제하지 않고 유일한 요구·규칙·장애 교훈을 추적한다.
일반 기능 작업의 정리는 변경 범위 주변으로 제한한다. 추적 파일 삭제는 정확한 대상,
참조·보존 조건·복구 근거·사람 승인이 모두 있어야 한다.

**Why this priority**: 정리의 편의보다 운영 지식과 복구 가능성이 우선이다.

**Independent Test**: 유일한 규칙, `.bak`, retention hold, 외부 참조가 있는 파일과
승인 후 변경된 파일을 넣어 보존·차단·격리된 삭제/복구 결과를 확인한다.

**Acceptance Scenarios**:

1. **Given** 기존 REQUEST/SPEC/PLAN 또는 기억의 유일한 주장, **When** 문서를 분리하면,
   **Then** 원본 유지 또는 정확한 목적지와 보존 근거가 section별로 연결된다.
2. **Given** 오래된 사용자 백업과 장애 기록, **When** 일반 정리를 하면,
   **Then** 자동 제거하지 않는다. 참조 검색 0건은 삭제 승인이 아니다.
3. **Given** 특정 hash의 삭제 승인, **When** 내용·HEAD·참조 또는 보존 조건이 달라지면,
   **Then** 삭제를 거부한다. 실제 저장소 전체 정리는 이 Work에서 실행하지 않는다.

### User Story 5 - Resume from authoritative shared work (Priority: P2)

다른 앱의 개발자는 채팅 요약 대신 공용 Work의 실제 상태로 이어서 작업한다.
완료된 Work의 설명을 읽는 행위는 다시 활성화하거나 다른 상태 저장소를 만들지 않는다.
작업 자격 증명은 일반 진행 출력에 포함하지 않는다.

**Independent Test**: 격리 Store에서 dependency 대기·완료·claim·재개·완료 후 view를
확인한다. 일반 heartbeat 출력에 synthetic credential이 없는지 검사한다.

**Acceptance Scenarios**:

1. **Given** dependency가 미완료, **When** 채팅에 완료라고 쓰더라도,
   **Then** downstream은 활성화되지 않는다. 실제 evidence와 상태 전이가 필요하다.
2. **Given** 실행 lease, **When** 일반 진행 상태를 출력하면,
   **Then** 자격 증명은 출력되지 않으며 정상 작업 갱신은 계속된다.

### Edge Cases

- symlink, 대소문자 충돌, 경로 이탈, 잘못된 schema, extra/missing package file.
- 부분 읽기, 대용량 한도, 접근할 수 없는 cross-repo 의존성, parse 오류.
- 정본이지만 stale인 문서, 오래됐지만 해당 release에 유효한 runbook, unknown lifecycle.
- source revision과 달라진 HTML, 같은 버전이지만 다른 payload, 중단된 install transaction.
- 악성 raw HTML·링크·제목·코드 예시, 비공개 문서로 연결된 공개 문서의 숨김 metadata.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: 기존 source/payload/installed 기능을 조사해 재사용한다. 공용 Kit는 앱의
  private 도메인 지식이나 Foundry Platform 기능에 의존하지 않는다. (`REQ-034`, `040`)
- **FR-002**: 새 저장소에 실제 baseline과 확장을 설치하고 기존 `work/design` 의미를
  보존한다. skill marker만으로 성공 처리하지 않는다. (`REQ-035`, `037`)
- **FR-003**: 설치·갱신·제거는 정확한 소유 범위와 이전 내용에 근거한다. 로컬 수정,
  host trust, 사용자 hook, Store binding을 보존하고 중단 시 복구한다. (`REQ-036`, `039`)
- **FR-004**: 버전명 외에 완전한 manifest·hash·실행 evidence로 package와 installed
  상태를 검증한다. 자동 fleet 배포나 임의 모델 선택은 하지 않는다. (`REQ-038`, `039`)
- **FR-005**: Work·Decision·Evidence·dependency의 정본은 기존 Project Store다.
  handoff는 projection이며 일반 출력은 lease를 드러내지 않는다. (`REQ-041`, native `E002`)
- **FR-006**: 소유 문서를 원본으로 유지한다. metadata는 한 곳에서 관리하고 목록과
  HTML은 파생한다. 수작업 파생본 수정은 drift로 잡는다. (`REQ-042`, `054`)
- **FR-007**: 문서 authority, lifecycle, freshness, security와 적용 release는 별개다.
  날짜만으로 폐기하지 않는다. 미분류는 검증된 정본이 아니다. (`REQ-043`)
- **FR-008**: 영향 검사는 삭제·이름 변경·문서만의 의미 변경·온톨로지 및 binding pin을
  포함한다. 미확인 범위가 있으면 complete가 아니며 gate를 통과하지 않는다. (`REQ-026`, `044`)
- **FR-009**: 검토 결과는 문서·의존 대상·후보 revision의 내용 hash와 검토자·방법·이유·
  evidence에 묶는다. 변경하지 않은 문서도 명시적인 검토 근거를 요구한다. (`REQ-045`)
- **FR-010**: 동일 topic·release·공개 범위의 active 정본 중복을 차단한다. 현행 검색과
  context에서 draft/candidate·unknown·stale·archived·superseded 문서를 제외한다.
  역사 검색은 명시 선택과 같은 보안 필터를 거친다. (`REQ-047`, `052`)
- **FR-011**: 기존 큰 문서의 분리는 section별 보존 ledger를 남긴다. 유일한 규칙과
  장애 교훈을 누락하거나 과거 결정을 현재 주장으로 덮어쓰지 않는다. (`REQ-048`)
- **FR-012**: tracked 삭제는 정확한 승인·참조 완전성·보존 조건·백업 검증을 요구한다.
  미승인 상태는 report-only다. 사용자 백업은 generated cache가 아니다. (`REQ-050`, `051`)
- **FR-013**: 일반 작업은 문서 갱신 후 review, 이후 변경 주변 gardening 순서를
  따른다. full cleanup은 별도 명시 Work이며 정리 뒤 재검증한다. (`REQ-075`, `082`)
- **FR-014**: HTML은 오프라인·스크립트 없이도 읽히고 raw 입력을 실행하지 않는다.
  접근성·인쇄·작은 화면과 private canary 검사를 통과한다. (`REQ-053`)
- **FR-015**: audience별 안내는 동일한 검토 source와 검증된 release set을 사용한다.
  제목·snippet·metadata를 만들기 전에 공개 범위를 걸러낸다. 사용자 문서에 내부 agent
  trace를 넣지 않는다. (`REQ-054`, `055`)

### Key Entities

- **Owned Package**: version, 전체 내용 식별자, 소유 파일·fragment, 이전 설치와 복구 기록.
- **Document Record**: qualified ID, owner, topic, 원본, 각 상태 차원, 의존 대상과 보존 조건.
- **Review Evidence**: 검토한 문서·dependency snapshot·후보 revision, 이유, 검토자, 방법과 근거.
- **Release Set**: UI·backend·contracts·ontology·Kit의 검증된 pin과 해당 문서 묶음의 연결.
- **Preservation Record**: 기존 section의 유일한 주장, 목적지 또는 원본 유지, 검증 근거.
- **Cleanup Candidate**: 정확한 대상·내용, 참조 완전성, 보존 조건, 사람 승인과 복구 근거.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 빈 저장소와 로컬 수정 저장소의 설치부터 제거·중단 복구까지 모든 필수
  시나리오가 통과하고 비소유 파일·설정 손실은 0건이다.
- **SC-002**: Astra에서 이 Work에 할당한 모든 positive/negative acceptance에 실제
  실행 근거 또는 명시적인 미검증 사유가 연결된다. 미검증은 PASS로 집계하지 않는다.
- **SC-003**: 동일 원본·선택 조건으로 만든 문서 묶음은 동일한 결과를 낸다.
  private canary 및 실행된 악성 입력은 0건이다.
- **SC-004**: 현행 검색·context·목록의 stale·superseded·미분류 canary 선택은 0건이다.
  적용 버전이 맞는 역사 자료는 opt-in에서 보존된다.
- **SC-005**: 기존 전체 회귀 검사와 세 관점의 독립 검토를 통과한다. 실제 host·운영
  환경에서 실행하지 않은 검증은 local fixture 결과와 분리한다.

## Assumptions

- W001과 W002는 native DONE이다. W002의 최종 고정 source는 변경하지 않는다.
  W003은 E021 승인 아래 RUNNING이며, S01 기반 검증과 원래 요구 전체의 완료를 구분한다.
- 이 Work의 범위는 로컬 Kit와 격리 canary다. 실제 fleet 갱신, Git publication,
  supervisor 실행, Store 재연결, 운영 배포와 실제 tracked 파일 삭제를 승인한 것이 아니다.
- Synapse 첫 배포는 기존 사내망·nginx 격리, 앱 자체 로그인 없음이다. 이 Work는
  별도 인증 서비스나 역할 관리 기능을 추가하지 않는다.
- Foundry의 기존 canonical Vault/Decision 승인 경계를 유지한다. 문서 lifecycle
  개선을 근거로 공식 지식이나 Platform storage schema를 임의 수정하지 않는다.
- 외부 공개 결과는 승인된 원본 묶음만 입력받는다. 내부 저장소를 checkout해 숨김
  표시하는 방법은 공개 분리로 인정하지 않는다.
- RHEL7 서버·VM은 없다. 운영 배포 증거는 W004의 별도 승인 실행에서만 얻을 수 있다.
