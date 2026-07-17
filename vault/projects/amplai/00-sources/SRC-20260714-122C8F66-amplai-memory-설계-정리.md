---
schema_version: 1
id: SRC-20260714-122C8F66
namespace: org/default/project/amplai
project: amplai
kind: source
status: active
title: AMPLAI Memory 설계 정리
summary: chatgpt에서 수집한 자유 형식 원문
created_at: 2026-07-14
updated_at: 2026-07-14
source_refs: []
relations: []
revision: 1
tags:
- chatgpt
source_metadata:
  source_type: chatgpt
  content_sha256: 122c8f66671a83167112121f7161d28f8b0a9872faf1794794adb137bb909b7a
  normalized_sha256: c5e223546e3b029d654df6c5168042669bf086e3db071da1c82b8669ae2a6dc1
  original_filename: amplai-memory-design-20260714.md
  media_type: text/markdown
  ingested_at: '2026-07-14T21:38:12.216638+09:00'
  created_by: user
---
# AMPLAI Memory 설계 정리

## Original Content
# AMPLAI Memory 설계 정리

## 1. 현재 구현 상태

`amplai-foundry`에는 다음 기반이 구현되어 있다.

- `MemoryObject` 모델
- Markdown 저장 어댑터
- `vault/` 지식 저장 구조
- 샘플 지식 문서 26개
- `lint`, `stats`, `show` CLI
- 초기 테스트 및 로드맵

현재 단계는 Markdown 지식을 AMPLAI가 관리할 수 있는 데이터로 변환하는 기초 단계다.

---

## 2. Markdown과 MemoryObject의 관계

Markdown은 사람이 읽고 작성하며 Git으로 변경 이력을 관리하는 지식 원본이다.

하지만 자유로운 Markdown 본문만으로는 프로그램이 다음을 확정적으로 판단하기 어렵다.

- 현재 유효한 지식인가?
- 어느 Tenant와 Project에 적용되는가?
- 기존 지식을 대체한 것인가?
- 정보의 출처와 근거는 무엇인가?
- 같은 지식이 이미 존재하는가?
- 검색 결과에 포함해도 되는가?

따라서 Markdown을 버리는 것이 아니라, Markdown을 정형화된 `MemoryObject`로 변환한다.

    Markdown
    → Markdown Adapter
    → MemoryObject
    → 검증·관계 관리·검색·인덱싱

---

## 3. MemoryObject의 역할

`MemoryObject`는 AMPLAI가 하나의 지식을 관리하는 표준 단위이자 도메인 계약이다.

예시:

    id: vbe-condition-002
    type: decision
    status: active

    scope:
      project: synapse
      product: NEFPT92A

    supersedes:
      - vbe-condition-001

    source:
      kind: engineer-decision
      reference: validation-report-023

주요 필드는 다음과 같다.

| 필드 | 역할 |
|---|---|
| `id` | 지식의 고유 식별자 |
| `type` | 결정, 사실, 절차, 사건 등의 지식 종류 |
| `status` | draft, active, superseded, archived 상태 |
| `scope` | Tenant, Workspace, Project, Environment 범위 |
| `content` | 실제 지식 내용 |
| `provenance` | 누가, 어디서, 어떤 근거로 생성했는지 |
| `links` | 관련 지식과의 연결 |
| `supersedes` | 어떤 이전 지식을 대체하는지 |
| `schema_version` | 해당 지식이 따르는 스키마 버전 |

`MemoryObject`는 Memory DB 자체가 아니다.

- Markdown: 현재 지식의 원본 저장 형식
- `MemoryObject`: AMPLAI 내부의 표준 지식 표현
- Memory DB: 향후 사용할 수 있는 영속 저장소
- Vector DB: 의미 기반 검색 인덱스

향후 저장 방식이 Markdown에서 PostgreSQL 등으로 바뀌어도 `MemoryObject` 계약은 유지할 수 있다.

---

## 4. 객체 형태가 검증에 유리한 이유

객체라는 사실 자체가 특별한 것은 아니다.

핵심은 자유로운 문장 속 의미를 고정된 필드로 명시하여 프로그램의 추측을 제거하는 것이다.

일반 문장:

    이 내용은 새로운 VBE 조건으로 교체되었다.

구조화된 관계:

    id: vbe-condition-002
    supersedes:
      - vbe-condition-001

이렇게 표현하면 프로그램이 다음을 기계적으로 검사할 수 있다.

- 참조한 ID가 실제로 존재하는가?
- 자기 자신을 대체하고 있지 않은가?
- A와 B가 서로를 대체하는 순환 관계가 있는가?
- 새 문서와 이전 문서가 모두 active 상태인가?
- 서로 다른 Tenant나 Project의 지식을 잘못 연결했는가?
- 필수 필드가 빠졌는가?
- 허용되지 않은 상태값을 사용했는가?

즉, 프로그램이 매번 자연어를 LLM으로 해석하지 않고 정해진 규칙으로 동일한 결과를 낼 수 있다.

---

## 5. 객체 형태가 검색과 인덱싱에 유리한 이유

사용자가 다음과 같이 질문한다고 가정한다.

    Synapse에서 현재 사용하는 VBE 측정 조건을 찾아줘.

구조화된 메타데이터가 있으면 먼저 정확한 조건으로 검색 범위를 줄일 수 있다.

    project == "synapse"
    type == "measurement-condition"
    status == "active"

그다음 남은 지식에 대해서만 키워드 또는 벡터 검색을 수행한다.

    메타데이터 필터
    → 키워드·벡터 검색
    → 관련도 정렬
    → LLM에 전달

이렇게 하면 폐기된 10nA 조건이나 다른 프로젝트의 VBE 조건이 LLM에 잘못 전달되는 것을 줄일 수 있다.

---

## 6. 시스템용 표현과 LLM용 표현의 분리

시스템은 구조화된 객체를 필요로 하고, LLM은 의미와 맥락이 포함된 자연어를 필요로 한다.

따라서 하나의 형식을 모든 단계에서 그대로 사용하지 않는다.

| 단계 | 권장 표현 | 목적 |
|---|---|---|
| 사람이 작성 | Markdown | 읽기와 편집 |
| 시스템 내부 | `MemoryObject` | 검증과 상태 관리 |
| 검색 결과 | `MemoryHit` | 검색 점수와 매칭 정보 |
| LLM 전달 단위 | `ContextItem` | 질문에 필요한 지식 한 건 |
| LLM 전달 묶음 | `ContextBundle` | 한 번의 요청에 사용할 전체 문맥 |

전체 흐름:

    Markdown
    → MemoryObject
    → 검증·검색
    → MemoryHit
    → ContextItem
    → ContextBundle
    → LLM

---

## 7. LLM에게 전달해야 하는 정보

LLM에는 단순한 사실뿐 아니라 다음 정보가 함께 전달되어야 한다.

    statement: VBE 측정은 I_TARGET=100nA를 사용한다.

    rationale:
      10nA에서는 장비 노이즈로 재현성이 낮았다.

    applicability:
      Synapse의 NEFPT92A 정규 측정

    exceptions:
      - Legacy 재현 시험에서는 10nA 사용

    evidence:
      validation-report-023

LLM에게 중요한 정보는 다음과 같다.

- 현재 유효한 결론
- 결정 또는 변경 이유
- 적용 범위
- 예외 조건
- 근거와 출처
- 과거 지식과의 관계

이 구조가 LLM의 지능을 높이는 것은 아니다.

LLM이 모호한 문서를 해석하는 데 사용하는 추론을 줄이고, 잘못된 범위 확대나 폐기된 정보 사용을 방지하는 역할을 한다.

---

## 8. MemoryPacket 용어 정정

`MemoryPacket`은 업계 표준 용어가 아니다.

앞선 설명에서 LLM에 전달하는 메모리 묶음을 표현하기 위해 사용한 임시 설계 명칭이다.

AMPLAI에서는 다음 명칭을 사용하는 것이 더 명확하다.

    MemoryObject
    = 저장되고 관리되는 지식 원본

    MemoryHit
    = 검색 점수와 함께 반환된 지식 한 건

    ContextItem
    = 질문에 맞게 LLM용으로 정리된 지식 한 건

    ContextBundle
    = 한 번의 LLM 요청에 전달되는 전체 문맥 묶음

`MemoryPacket`은 향후 MCP나 A2A를 통해 시스템 간 메모리를 전송하는 실제 운반 객체가 필요할 때 다시 검토한다.

---

## 9. 다음 구현 단계

다음 마일스톤은 `Knowledge Integrity Pipeline v0.1`이다.

### 9.1 MemoryObject 스키마 단일화

- Pydantic 모델을 유일한 스키마 원본으로 지정
- Markdown front matter 검증도 같은 모델 사용
- 필요하면 Pydantic 모델에서 JSON Schema 생성
- Tenant → Workspace → Project → Environment 범위 확정
- 허용되는 type과 status 값 확정

### 9.2 지식 생명주기 구현

기본 상태 흐름:

    draft
    → active
    → deprecated 또는 superseded
    → archived

필요한 관계와 정보:

- `supersedes`
- `superseded_by`
- 상태 전이 규칙
- 변경 이유
- 출처와 근거
- 생성 및 변경 시점

### 9.3 Knowledge Linter 강화

검사 대상:

- 중복 ID
- 필수 front matter 누락
- 허용되지 않은 상태값
- 존재하지 않는 지식 참조
- 깨진 링크
- 자기 자신 참조
- 순환 참조
- 범위를 벗어난 관계
- 이전 문서와 신규 문서의 동시 active
- superseded 관계와 status 불일치

### 9.4 안전한 쓰기 흐름 구현

필요한 CLI:

- `amplai ingest`
- `amplai source`
- `amplai proposal`

AI가 기존 지식을 바로 덮어쓰지 않도록 한다.

기본 흐름:

    새 정보 입력
    → 관련 기존 지식 검색
    → 중복 및 충돌 검사
    → 변경 proposal 생성
    → 사람 또는 정책의 승인
    → 신규 지식 active 처리
    → 기존 지식 superseded 처리
    → 전체 vault lint 실행

### 9.5 CI Gate 추가

필수 검증:

- format
- code lint
- type check
- unit test
- knowledge lint

하나라도 실패하면 변경을 병합하지 않는다.

---

## 10. 최종 설계 결정

AMPLAI Memory는 다음 원칙으로 설계한다.

1. Markdown을 사람이 관리하는 지식 원본으로 사용한다.
2. Markdown을 `MemoryObject`로 변환하여 스키마, 상태, 관계와 출처를 검증한다.
3. 검색 결과 한 건은 `MemoryHit`으로 표현한다.
4. 검색한 지식을 LLM이 이해하기 좋은 `ContextItem`으로 렌더링한다.
5. 여러 ContextItem을 `ContextBundle`로 조립하여 Harness가 LLM에 전달한다.
6. AI는 기존 지식을 직접 덮어쓰지 않고 proposal과 승인 절차를 거친다.
7. RAG, Vector DB, MCP로 넘어가기 전에 지식 생명주기와 정합성 검증을 완성한다.

현재 구현 우선순위:

    MemoryObject 계약 확정
    → Knowledge Integrity Pipeline
    → 지식 생명주기와 변경 승인
    → 로컬 검색 및 인덱싱
    → MemoryHit
    → ContextItem / ContextBundle
    → Harness와 LLM 연결
    → Memory DB / Vector DB
    → MCP 및 A2A 제공
    → Meta-loop 기반 지식 품질 개선
