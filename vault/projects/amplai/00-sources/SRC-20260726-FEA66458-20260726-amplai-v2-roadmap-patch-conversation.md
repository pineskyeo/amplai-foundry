---
schema_version: 1
id: SRC-20260726-FEA66458
namespace: org/default/project/amplai
project: amplai
kind: source
status: active
title: 20260726-amplai-v2-roadmap-patch-conversation
summary: chatgpt에서 수집한 자유 형식 원문
created_at: 2026-07-26
updated_at: 2026-07-26
source_refs: []
relations: []
revision: 1
tags:
- chatgpt
source_metadata:
  source_type: chatgpt
  content_sha256: fea664582ee0d84e04ea08871465312cb019acc1226074089690cba4d14f5986
  normalized_sha256: 187a5d911d45ff496d7f5355aeaef625047f02f0c34072f893a449995876e682
  original_filename: 20260726-amplai-v2-roadmap-patch-conversation.md
  media_type: text/markdown
  ingested_at: '2026-07-26T19:28:22.728311+09:00'
  created_by: user
---
# 20260726-amplai-v2-roadmap-patch-conversation

## Original Content
AMPLAI v2 로드맵 패치 이후 대화 상세 정리

• 기준 시점: AMPLAI_MASTER_ROADMAP_PROPOSAL_v2.md 및 amplai-foundry-main-roadmap-v2.zip 생성 이후
• 정리 범위:
  1. AMPLAI를 실행하는 외부 Agent Host로서 Hermes의 역할
  2. AMPLAI 서버와 상주 구성요소
  3. Knowledge Steward의 실행 방식
  4. LLM 기반 지식 저장의 비결정성 위험
  5. Governed Knowledge Compiler 구조
  6. 로드맵에 반영해야 할 추가 설계 원칙과 구현 우선순위

────────

1. 대화의 출발점

v2 로드맵은 사용자가 저장 위치, 지식 유형, ID, 관계를 일일이 지정하지 않고 다음처럼 의도만 전달하도록 방향을 수정했다.

> “이 로드맵을 AMPLAI에 반영해줘.”

AMPLAI가 내부적으로 수행해야 하는 흐름은 다음과 같이 정의됐다.

```text
입력 수신
→ 프로젝트 판별
→ Source 원본 보존
→ 문서 유형 및 지식 유형 분류
→ 기존 지식과 중복·충돌 비교
→ Canonical 변경 Proposal 생성
→ Roadmap Tracker 갱신
→ Lint/Test
→ 중요한 승인 사항만 사용자에게 제시
```

이후 대화에서는 이 자동화된 지식 수집과 실행을 실제로 누가 담당할 것인지, 서버에 무엇이 상주해야 하는지, LLM의 비결정성을 어떻게 통제할 것인지 구체화했다.

────────

2. Hermes를 AMPLAI 실행 Agent로 사용할 수 있는가

2.1 결론

Hermes를 AMPLAI의 첫 번째 실행 인터페이스이자 외부 Agent Host로 사용하는 방향은 적합하다.

다만 다음과 같이 구분해야 한다.

```text
Hermes ≠ AMPLAI
Hermes = AMPLAI를 사용하는 Client Partner + Agent Host
```

Hermes 안에 AMPLAI 전체를 종속시키는 것이 아니라, Hermes가 독립된 AMPLAI Runtime의 기능을 호출하는 구조가 권장된다.

2.2 역할 분리

Hermes

Hermes는 사용자와 AMPLAI 사이의 대화 및 작업 조율 계층이다.

주요 책임:

• 자연어 요청 접수
• 사용자 의도 파악
• 대화 세션과 개인 선호 관리
• 메신저·CLI·Desktop 등의 사용자 채널 제공
• AMPLAI Capability 호출
• Claude Code, Codex, 사내 GLM 등 전문 Agent에 작업 위임
• 결과 통합 및 사용자 전달
• 일정, 알림, 반복 업무의 개인 비서 기능

예:

```text
사용자:
“이 로드맵을 AMPLAI에 반영해줘.”

Hermes 해석:
intent = adopt_roadmap
project = amplai
input = roadmap-v2.md
```

Hermes는 이후 AMPLAI Gateway에 구조화된 요청을 전달한다.

AMPLAI

AMPLAI는 프로젝트와 도메인의 공식 지능 기반이다.

주요 책임:

• Project Identity와 Scope
• Canonical Memory
• Domain Ontology
• Project Fact
• Source 및 Provenance
• Context Runtime
• Policy와 Governor
• Proposal과 Review
• Evaluation Observatory
• Meta-loop

즉, Hermes가 꺼지거나 다른 Agent Framework로 교체돼도 AMPLAI의 지식과 정책은 유지돼야 한다.

Claude Code / Codex / 사내 GLM

이들은 전문 실행자 또는 Worker Agent다.

예:

• Claude Code: 복잡한 코드 분석, 구현, 리뷰
• Codex: 코드 수정, 테스트, 반복 구현
• 사내 GLM: 기밀 문서와 로그의 사내 처리
• Reviewer Agent: 구현 결과 검증
• Evaluator Agent: 품질 점수 및 실패 유형 평가

Governor

Governor는 다음을 통제한다.

• 정책 검사
• 위험도 분류
• 자동 적용 가능 여부
• 사용자 승인 필요 여부
• Production Action 차단
• Canonical Knowledge 승격
• Ontology Breaking Change 검토

2.3 권장 전체 구조

```text
사용자 / 팀 메신저 / CLI / Desktop
                  │
                  ▼
┌────────────────────────────────────┐
│ Hermes                             │
│ Client Partner / Personal Agent    │
│ Session / Scheduling / Delegation  │
└─────────────────┬──────────────────┘
                  │ MCP / HTTP / CLI
                  ▼
┌────────────────────────────────────┐
│ AMPLAI Gateway / Runtime           │
│ project.resolve                    │
│ context.build                      │
│ memory.search                      │
│ ontology.resolve                   │
│ knowledge.ingest                   │
│ proposal.create                    │
│ policy.check                       │
│ evaluation.submit                  │
└───────────────┬────────────────────┘
                │
       ┌────────┴────────┐
       ▼                 ▼
┌───────────────┐  ┌─────────────────┐
│ Foundry       │  │ Agent Harness   │
│ Memory        │  │ Loop / Policy   │
│ Ontology      │  │ Tool / Governor │
│ Proposal      │  └────────┬────────┘
└───────────────┘           ▼
                    Claude / Codex / GLM
```

2.4 Hermes Memory와 AMPLAI Memory의 경계

Hermes의 개인 메모리와 AMPLAI의 공식 지식은 분리해야 한다.

Hermes Memory에 적합한 정보:

• 사용자 응답 스타일
• 개인 일정
• 자주 사용하는 명령
• 최근 대화 맥락
• 채널 설정
• 알림 방식
• 개인적인 업무 습관
• 단기 실행 힌트

AMPLAI에 저장해야 하는 정보:

• 프로젝트 Architecture
• 공식 Decision
• Domain Ontology
• 업무 개념과 관계
• 시스템 Policy
• 승인된 Roadmap
• 검증된 장애 원인
• Recipe·Run·Equipment 의미
• 평가 및 개선 이력
• Provenance가 있는 공식 지식

Hermes가 대화 중 발견한 업무 지식은 바로 공식 지식으로 저장되지 않는다.

```text
Hermes Observation
→ AMPLAI Source
→ Knowledge Candidate
→ 중복·충돌 분석
→ Proposal
→ Review / Governor
→ Canonical Knowledge
```

2.5 AMPLAI는 Hermes에 종속되면 안 된다

초기에는 Hermes를 우선 Client Partner로 채택할 수 있지만 AMPLAI의 핵심은 독립 서비스여야 한다.

```text
Hermes          ┐
Claude Desktop  │
Codex CLI       ├─→ AMPLAI Server
사내 메신저 Bot │
Web UI          ┘
```

장기 Architecture Decision 후보:

> Hermes를 AMPLAI의 우선 Client Partner 및 External Agent Host로 채택하되, Canonical Memory, Ontology, Policy, Context Runtime, Evaluation은 독립 서비스로 유지한다.

────────

3. AMPLAI 지식을 저장하는 Agent는 서버에 상주해야 하는가

3.1 결론

항상 상주해야 하는 것은 자유롭게 사고하는 LLM Agent가 아니라 AMPLAI Runtime 서비스다.

```text
항상 상주:
- AMPLAI API / Gateway
- 프로젝트 및 메모리 저장소
- Ontology Registry
- 검색 서비스
- Proposal Manager
- Event / Job Queue
- 인증·정책·감사 로그

요청 시 실행:
- Knowledge Steward LLM
- Context Builder
- 중복·충돌 의미 분석
- Evaluator
- Meta-loop Analyst
```

3.2 권장 Runtime 구조

```text
사용자 / Hermes
       │
       ▼
AMPLAI API
├─ Project Resolver
├─ Source Intake
├─ Memory Repository
├─ Ontology Registry
├─ Proposal Manager
├─ Policy / Governor
└─ Job Queue
       │
       ▼ 필요할 때만 실행
Knowledge Steward Worker
├─ 입력 해석
├─ 지식 후보 추출
├─ 관계 제안
├─ 중복·충돌 의미 분석
└─ Proposal 작성
```

Knowledge Steward는 24시간 대기하며 자유롭게 행동하는 인격형 Agent가 아니다. AMPLAI Server가 요청 또는 이벤트 발생 시 시작하는 통제된 작업 역할이다.

3.3 입력 처리 예시

사용자:

> “이 문서를 Cortex 지식에 반영해줘.”

처리 흐름:

```text
1. Hermes가 사용자 요청을 AMPLAI Server로 전달
2. AMPLAI가 Source 원본을 먼저 저장
3. Job Queue에 knowledge_intake 작업 생성
4. Worker가 LLM을 호출하여 후보를 추출
5. 결정론적 코드가 ID·경로·Schema·관계를 검증
6. 중복·충돌을 분석
7. Proposal을 생성
8. 안전 변경은 자동 적용
9. 중요한 의미 변경은 사용자 또는 Governor 승인 요청
10. Worker 종료
```

3.4 LLM Agent를 계속 상주시킬 필요가 없는 이유

상주형 자율 LLM은 다음 문제를 만든다.

• 지속 비용
• 컨텍스트 오염
• 비의도적 자기 행동
• 복잡한 상태 복구
• 모델 종속성
• 통제되지 않은 지식 변경 가능성

권장 방식:

```text
평상시:
AMPLAI Runtime 대기

이벤트 발생:
Agent Run 생성
→ 처리
→ 결과 저장
→ 종료
```

3.5 개인 서버 초기 배포 형태

초기에는 Mac mini 또는 개인 Linux 서버에서 단순하게 운영할 수 있다.

```text
hermes-gateway      # 선택적 상주
amplai-api          # 상주
amplai-worker       # 요청 시 작업
amplai-scheduler    # 주기 작업
Git / File Store    # Canonical 원본
SQLite              # Queue, Search, Runtime
```

초기 단계에서 필요하지 않은 것:

• Kubernetes
• Kafka
• 대형 Graph DB
• 복잡한 분산 Agent Cluster

필요성이 입증되기 전까지 Modular Monolith 또는 단일 서버 구성이 적절하다.

3.6 Meta-loop도 상주형 자유 Agent가 아니다

Meta-loop는 지속적으로 자기 코드를 바꾸는 Agent가 아니다.

```text
Agent Run 기록
→ 실패·피드백·점수 누적
→ Trigger 조건 충족
→ Meta-loop 분석 작업 실행
→ 개선 가설 생성
→ Sandbox / Replay 평가
→ Proposal
→ Governor Review
→ 승인 적용 또는 Rollback
→ 종료
```

Trigger 예:

• 동일 실패가 일정 횟수 이상 반복
• 사용자 수정 피드백 발생
• 회귀 점수 하락
• 정기 평가 시점 도래
• 비용 또는 latency 급증

────────

4. LLM으로 지식을 저장할 때의 비결정성 문제

4.1 사용자의 우려

LLM은 같은 입력에 대해서도 실행마다 다르게 해석할 수 있다.

발생 가능한 문제:

• Concept를 Decision으로 잘못 분류
• 원문에 없는 결론 추가
• 중요한 제약 누락
• 기존 지식과 다른 관계 연결
• 같은 지식을 새로운 이름으로 중복 생성
• 모델 변경 후 저장 형식 변화
• 과도한 일반화
• 임시 아이디어를 공식 원칙으로 승격
• 질문을 결정으로 변환

이 우려는 정확하며, AMPLAI에서 반드시 최우선으로 통제해야 한다.

4.2 핵심 설계 원칙

```text
LLM
= 지식 후보와 관계를 제안하는 해석기

AMPLAI 결정론적 코드
= ID, 저장 위치, 형식, 검증, 중복 처리, Rendering

Governor
= 공식 승격과 고위험 의미 변경 통제
```

따라서 LLM은 Canonical Memory를 직접 쓰거나 수정할 권한을 가져서는 안 된다.

────────

5. Governed Knowledge Compiler

이 문제를 해결하기 위한 핵심 구조는 Governed Knowledge Compiler다.

```text
Immutable Source
      ↓
Extractor LLM
      ↓
Candidate Store
      ↓
Deterministic Validator
      ↓
Dedup / Conflict Engine
      ↓
Proposal Store
      ↓
Governor
      ↓
Deterministic Canonical Renderer
```

이 구조는 자연어를 곧바로 공식 지식으로 저장하지 않는다. 자연어를 컴파일 입력처럼 취급하고, Schema·증거·정책·검증을 거쳐 공식 자산으로 생성한다.

────────

6. 단계별 안전 장치

6.1 원본 Source를 먼저 불변 저장

LLM 분석 전에 원문을 그대로 저장한다.

```yaml
source_id: SRC-20260726-001
project: amplai
authority: user
received_at: 2026-07-26T...
content_hash: sha256:...
content_type: conversation
```

핵심 원칙:

> 공식 증거는 LLM 요약이 아니라 원본 Source다.

LLM 분석이 잘못돼도 Source를 기준으로 재분석할 수 있다.

6.2 LLM 출력은 자유 Markdown이 아니라 고정 Schema

LLM이 파일명, ID, Markdown Frontmatter를 임의로 생성하게 하지 않는다.

```json
{
  "candidates": [
    {
      "candidate_type": "principle",
      "statement": "LLM output is never canonical by itself.",
      "evidence": [
        {
          "source_id": "SRC-20260726-001",
          "start_line": 41,
          "end_line": 44
        }
      ],
      "confidence": 0.94,
      "suggested_action": "create_candidate"
    }
  ]
}
```

허용 유형은 시스템이 고정한다.

```text
source
concept
principle
decision
architecture
question
experiment
roadmap_update
ignore
```

LLM은 새로운 자산 유형이나 폴더 구조를 임의로 만들 수 없다.

6.3 모든 Candidate에 근거 연결 필수

Candidate 필수 필드:

```text
statement
source_id
evidence range
confidence
interpretation notes
extractor version
```

거부 조건:

• 근거 위치 없음
• 원문에 없는 단정
• 원문보다 의미가 강함
• 서로 모순되는 근거
• Source와 Candidate의 의미 불일치

예:

```text
원문:
“초기에는 Hermes 사용을 검토할 수 있다.”

잘못된 후보:
“AMPLAI는 Hermes를 영구 표준 Agent로 채택한다.”
```

이 후보는 원문의 의미를 과도하게 강화했으므로 탈락해야 한다.

6.4 분류 규칙을 정책으로 고정

기본 분류 원칙:

```text
개념의 정의
→ Concept

장기적으로 지켜야 할 방향
→ Principle

명시적 선택과 적용 범위
→ Decision

구조·경계·컴포넌트 관계
→ Architecture

해결되지 않은 쟁점
→ Question

검증해야 할 가설
→ Experiment

단계·우선순위·완료 조건
→ Roadmap Update
```

특히 Decision은 엄격하게 판단한다.

Decision 승격 조건:

• 사용자가 명시적으로 선택했는가
• 선택 대상과 대안이 존재하는가
• 적용 범위가 명확한가
• 기존 Decision과 충돌하지 않는가
• 증거가 충분한가

불명확하면 Decision으로 만들지 않고 Question 또는 Proposal 상태로 둔다.

6.5 Idempotency 보장

같은 입력을 여러 번 넣어도 중복 자산이 생성되지 않아야 한다.

```text
Source Hash 동일
→ 새 Source 생성 안 함
→ 기존 처리 이력 표시
→ 필요 시 새 Extraction Run만 생성
```

Candidate 비교 결과 유형:

```text
create_new
update_existing
link_only
duplicate
conflict
supersede_candidate
```

표현이 달라도 의미가 같은지 비교해야 한다.

```text
신규 후보:
“LLM은 공식 지식을 직접 수정하지 않는다.”

기존 원칙:
“Canonical assets may only be changed through governed proposals.”
```

동일 의미라면 새 Principle을 만들지 않고 기존 Principle에 Source 관계만 추가한다.

6.6 위험도별 자동화 수준

자동 적용 가능

• Immutable Source 저장
• Hash 계산
• ID 발급
• Source 중복 탐지
• Question Candidate 생성
• 단순 Roadmap 상태 갱신
• Map 관계 추가
• Derived Index 재생성
• Lint/Test

Proposal까지만 자동

• Concept 신규 생성
• Principle 신규 생성
• Architecture 관계 변경
• 기존 지식 보완
• Ontology Term 추가

반드시 승인 필요

• Decision 확정
• 기존 Decision 변경 또는 폐기
• Architecture Boundary 변경
• Ontology 의미 변경
• Policy 및 권한 변경
• 자동 실행 범위 확대
• Production 관련 지식
• Breaking Change

사용자는 저장 구조가 아니라 중요한 의미 변경만 검토한다.

6.7 동일 입력에 대한 분석 불일치 처리

동일 Source를 두 번 분석했는데 결과가 다를 수 있다.

```text
Run A:
Principle 2
Question 1

Run B:
Principle 1
Decision 1
```

이 경우 자동으로 Decision을 채택하지 않는다.

```text
classification_disagreement
→ review_required
```

고위험 입력에는 여러 단계 분석을 사용할 수 있다.

```text
Extractor
→ Candidate 추출

Critic
→ 원문보다 과도한 해석인지 검사

Resolver
→ 분류 불일치 정리

Deterministic Validator
→ Schema·Policy·Relation 검사
```

단, 여러 LLM이 동의했다고 해서 공식 진실이 되는 것은 아니다. 최종 기준은 원본 Source, 정책, 명시적 권위다.

6.8 분석 Run의 재현 정보 저장

모든 Extraction Run은 다음 정보를 저장해야 한다.

```yaml
extractor:
  model: company-glm-5.1
  model_revision: ...
  prompt_version: knowledge-extractor-v3
  ontology_version: dc-test-0.2.0
  policy_version: knowledge-policy-1.1
  schema_version: candidate-1.0
  temperature: 0
```

temperature: 0도 완전한 동일 결과를 보장하지 않는다.

따라서 재현성은 모델 출력의 동일성에 의존하지 않고 다음으로 확보한다.

• Source 원본
• Content Hash
• Model Version
• Prompt Version
• Ontology Version
• Policy Version
• Extraction Result
• Proposal
• Review Decision
• Final ChangeSet

6.9 Canonical 파일은 Renderer가 생성

LLM은 구조화된 후보만 생성한다.

```json
{
  "candidate_type": "principle",
  "title": "LLM is not the canonical authority",
  "statement": "...",
  "evidence": ["SRC-...#L41-L44"]
}
```

AMPLAI의 결정론적 Renderer가 공식 템플릿으로 파일을 만든다.

```markdown
---
id: PRI-0027
type: principle
status: candidate
namespace: project/amplai
derived_from:
  - SRC-20260726-001
---

# LLM is not the canonical authority

## Statement

...

## Evidence

- SRC-20260726-001, lines 41–44
```

이렇게 하면 모델별 파일 구조 차이, ID 충돌, Frontmatter 누락을 방지할 수 있다.

────────

7. Knowledge Steward 회귀 평가

Knowledge Steward 자체에도 Golden Set과 Evaluation이 필요하다.

대표 입력 유형:

• 명백한 Concept
• 명백한 Decision
• 질문만 있는 문서
• 중복 지식 문서
• 기존 Decision과 충돌하는 문서
• 과도한 추론을 유도하는 문서
• 여러 프로젝트가 섞인 문서
• 임시 메모와 공식 결정이 혼합된 문서
• Roadmap 변경 문서
• Incident Report

기대 결과 예:

```yaml
expected:
  source_created: true
  candidates:
    concepts: 1
    decisions: 0
    questions: 2
  requires_approval: false
  forbidden:
    - canonical_auto_apply
    - unsupported_inference
```

모델·프롬프트·Ontology·Policy를 변경할 때마다 Golden Set을 재실행해야 한다.

이는 AMPLAI의 Loop Engineering과 직접 연결된다.

────────

8. 로드맵에 추가해야 할 공식 원칙 후보

Principle 후보

PRI — LLM output is never canonical by itself

LLM 출력은 그 자체로 공식 지식이 될 수 없다.

PRI — Every knowledge candidate must be traceable to immutable evidence

모든 지식 후보는 변경되지 않는 원본 증거와 연결돼야 한다.

PRI — Canonical rendering and identity assignment are deterministic

Canonical 파일 생성, ID, 저장 위치, Metadata는 결정론적 코드가 담당한다.

PRI — Ambiguity must remain explicit

불확실한 내용을 Decision으로 강제하지 않고 Question 또는 Experiment로 보존한다.

PRI — High-impact semantic changes require governed approval

Decision, Architecture, Ontology, Policy의 중요한 변경은 승인 절차를 거친다.

PRI — Knowledge intake must be replayable and regression-tested

동일 Source를 새로운 모델과 정책으로 재분석할 수 있어야 하며, 변경 전 회귀 평가를 통과해야 한다.

PRI — The always-on component is the runtime, not an autonomous LLM

상주해야 하는 것은 AMPLAI Runtime이며, LLM Agent는 요청과 Trigger에 따라 실행된다.

PRI — Client agents are replaceable

Hermes 등 Client Agent는 교체 가능해야 하며 AMPLAI의 지식 자산은 독립적으로 유지돼야 한다.

────────

9. Architecture 후보

ARC — Client Partner and AMPLAI Runtime Boundary

```text
Hermes:
대화, 개인 비서, 메신저, 세션, 위임

AMPLAI:
Project, Memory, Ontology, Policy, Proposal, Evaluation
```

ARC — Event-driven Knowledge Steward

Knowledge Steward는 상주 자율 Agent가 아니라 Job Queue에 의해 실행되는 Worker다.

ARC — Governed Knowledge Compiler

```text
Source
→ Extractor
→ Candidate
→ Validator
→ Dedup/Conflict
→ Proposal
→ Governor
→ Renderer
```

ARC — Canonical and Runtime Separation

```text
Canonical:
Git-backed Memory / Ontology / Policy

Runtime:
Queue / Cache / Session / Extraction Run / Event
```

ARC — Replaceable External Agent Host

Hermes를 첫 Client Partner로 사용하되 AMPLAI API는 Hermes와 독립적인 계약으로 유지한다.

────────

10. Decision 후보

DEC 후보 — Hermes First, Not Hermes Locked

Hermes를 AMPLAI의 첫 External Agent Host와 Client Partner로 사용하지만 AMPLAI를 Hermes에 종속시키지 않는다.

DEC 후보 — Event-driven Steward

Knowledge Steward LLM은 상주하지 않고 요청 또는 Trigger 발생 시 실행한다.

DEC 후보 — No Direct Canonical Write by LLM

LLM에 Canonical Repository 직접 수정 권한을 부여하지 않는다.

DEC 후보 — Deterministic Canonical Renderer

공식 파일은 AMPLAI 코드가 고정 Schema와 Template로 생성한다.

DEC 후보 — Evidence-required Candidate

원본 근거가 없는 Candidate는 Proposal 대상이 될 수 없다.

────────

11. 열린 질문

Q1. Knowledge Steward의 첫 LLM은 무엇을 사용할 것인가

후보:

• Claude
• Codex
• 사내 GLM
• 모델별 라우팅
• 민감도에 따른 Hybrid Routing

평가 기준:

• 한국어 업무 문서 정확도
• Schema 준수율
• 근거 위치 추출 정확도
• 비용
• 보안
• 반복 실행 안정성

Q2. Candidate 의미 중복 판정은 어떻게 구성할 것인가

가능한 조합:

```text
정확 문자열
+ Canonical Alias
+ Ontology Term
+ Embedding Similarity
+ LLM Semantic Judge
+ Human Review
```

한 가지 방식만 사용해서는 안 된다.

Q3. Source 저장소와 Canonical 저장소는 같은 Git Repository인가

초기에는 같은 저장소의 다른 경로로 구성할 수 있다.

장기적으로는 대용량 원문, 보안 등급, 개인정보, 사내 문서, Binary Artifact 때문에 별도 Source Store가 필요할 수 있다.

Q4. 자동 승인 범위는 어디까지 허용할 것인가

초기에는 매우 보수적으로 설정하는 것이 적절하다.

```text
자동 적용:
Source, Hash, Index, Question, Map

승인 필요:
Concept, Principle, Architecture, Decision, Ontology
```

평가 결과에 따라 자동 적용 범위를 점진적으로 확장할 수 있다.

Q5. Hermes의 개인 메모리 중 어떤 내용을 AMPLAI Intake 대상으로 올릴 것인가

모든 대화를 자동 수집하면 지식 오염과 보안 문제가 발생한다.

필요한 Trigger 예:

• 사용자가 “기록해줘”라고 명시
• 반복 업무에서 장기 지식 후보 탐지
• 구현 완료 후 Decision 또는 Architecture 변화 탐지
• Incident 해결 후 재사용 가능한 지식 탐지
• 주간 리뷰에서 승격 후보 선별

────────

12. 수정된 구현 우선순위 제안

Phase 1A — Minimal Project Identity

• Project Scope 결정
• Namespace + Local ID
• Source와 Candidate의 프로젝트 귀속
• Cross-project 참조 검증

Phase 1B-1 — Immutable Source Intake

• 파일 또는 대화 수신
• Source 원본 저장
• Hash, Authority, Project, Timestamp 기록
• 중복 Source 탐지

이 단계는 LLM 없이 먼저 완성할 수 있다.

Phase 1B-2 — Structured Candidate Extraction

• 고정 JSON Schema
• Evidence Range 필수
• 허용 Candidate Type 제한
• Extraction Run Metadata 저장
• LLM은 Candidate Store만 작성

Phase 1B-3 — Deterministic Validation and Rendering

• ID 발급
• 경로 선정
• Schema Lint
• Relation Validation
• Canonical Renderer
• LLM 직접 파일 수정 금지

Phase 1B-4 — Dedup, Conflict, and Proposal

• 중복 판정
• 충돌 판정
• Existing Knowledge Update 제안
• Proposal ChangeSet 생성
• 위험도 분류

Phase 1B-5 — Governor and Approval UX

사용자에게 파일 구조가 아니라 의미 변경만 보여준다.

```text
자동 처리 완료:
- Source 1건
- 기존 Principle 관계 추가 2건
- Roadmap Tracker 갱신

승인 필요:
- 새 Decision 후보 1건
- 기존 Architecture 보완 1건

충돌:
- DEC-0007과 적용 범위 불일치
```

Phase 1B-6 — Knowledge Intake Golden Set

• 대표 입력 Dataset
• 분류 정확도 평가
• Unsupported Inference 검사
• 중복 생성률
• Evidence 정확도
• Regression Gate

Phase 1C — Full Project Pack

Knowledge Intake가 안정화된 후 다음을 완성한다.

• .amplai/project.yaml
• domain.lock
• Project-local Memory
• Project-local Ontology Extension
• Skills
• Policies
• Evals
• Runtime 분리
• 이동성과 복구 가능성

Phase 2 이후

```text
Ontology Kernel
→ DC Test E2E Vertical Slice
→ Context Runtime
→ AMPLAI Gateway / MCP
→ Hermes Integration
→ Development Agent
→ Management Agent
→ Evaluation Observatory
→ Meta-loop
→ Central / Hybrid Deployment
```

Hermes 통합을 너무 앞당겨 AMPLAI의 핵심 계약이 Hermes 전용으로 굳지 않도록 주의해야 한다.

────────

13. 첫 E2E Vertical Slice 제안

다음 한 문장을 완전하게 처리하는 것이 첫 성공 기준이다.

> “이 문서를 Cortex 프로젝트 지식으로 반영해줘.”

입력:

• 사용자 요청
• Markdown 문서 또는 대화
• 현재 Project Context

기대 흐름:

```text
1. Project = cortex 판별
2. Source 불변 저장
3. Source Hash와 Authority 기록
4. Candidate Schema 추출
5. Evidence Range 검증
6. 기존 Cortex 지식 검색
7. 중복·충돌 분류
8. Proposal 생성
9. 안전 변경 자동 적용
10. Decision/Architecture 변경 승인 요청
11. Canonical Renderer 실행
12. Knowledge Lint
13. Evaluation 기록
14. 사용자에게 의미 단위 결과 제공
```

실패 조건:

• LLM이 Canonical 파일 직접 수정
• Source 근거 없이 Candidate 생성
• 같은 입력으로 중복 Note 생성
• 질문을 Decision으로 자동 승격
• 기존 Decision을 조용히 덮어씀
• 모델 또는 Prompt Version 미기록
• 사용자에게 저장 폴더와 ID를 직접 지정하도록 요구

────────

14. 최종 정리

v2 패치 이후 대화에서 AMPLAI의 실행 및 지식 저장 구조는 다음처럼 구체화됐다.

```text
Hermes
= 사용자 접점과 Client Partner

AMPLAI Runtime
= 항상 접근 가능한 프로젝트 지능 서버

Knowledge Steward
= 요청 시 실행되는 지식 해석 Worker

LLM
= Candidate 제안자

Deterministic System
= ID, 저장 위치, 검증, Rendering 담당

Governor
= 고위험 의미 변경 승인

Canonical Memory
= 검토되고 출처가 보존된 공식 지식

Meta-loop
= 실행 결과를 평가하고 개선 Proposal을 만드는 통제된 반복 구조
```

가장 중요한 결론은 다음 두 가지다.

> 항상 상주해야 하는 것은 자유롭게 생각하는 LLM이 아니라, 프로젝트 지식·정책·Queue·검증을 제공하는 AMPLAI Runtime이다.

> LLM은 지식을 직접 저장하지 않고, 불변 Source를 근거로 Candidate를 제안하며, 공식 지식은 결정론적 검증과 Governor를 거쳐 생성된다.

사용자는 여전히 다음처럼 간단히 요청할 수 있다.

```text
“이 내용을 AMPLAI에 반영해줘.”
```

그러나 내부적으로는 단순한 LLM 요약 저장이 아니라 다음이 실행된다.

```text
Source
+ Schema
+ Evidence
+ Dedup
+ Conflict Analysis
+ Policy
+ Proposal
+ Approval
+ Deterministic Rendering
+ Regression Evaluation
```

이것이 AMPLAI가 지향해야 할 Governed Knowledge Compiler 기반의 지식 축적 시스템이다.
