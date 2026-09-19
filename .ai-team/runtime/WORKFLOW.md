# AMPLAI Loop Runtime V2 Workflow

## 1. `/work` state machine

```text
INTAKE / RESUME
  ↓
CLASSIFY ───────── high/decision missing ─────→ DESIGN GATE
  ↓
CONTRACT
  ↓
KNOWLEDGE READINESS
  ├─ READY/BYPASS → CONTEXT PACK
  ├─ DISCOVER     → DOMAIN DISCOVERY → READINESS
  └─ BLOCKED      → HUMAN DECISION / STOP
  ↓
ENVIRONMENT FINGERPRINT
  ↓
PLAN / TASKIFY / ANALYZE
  ↓
┌──────────────── SLICE LOOP ──────────────────┐
│ EXECUTE → DETERMINISTIC VERIFY               │
│               ├─ PASS → EVIDENCE → NEXT     │
│               └─ FAIL → DIAGNOSE → REPAIR ──┘
└───────────────────────┬──────────────────────┘
                        ↓
                    CONVERGE
                  gap ├─ yes → SLICE LOOP
                      └─ no
                        ↓
             DOCUMENTATION FRESHNESS
       impacted docs ├─ yes → patch/supersede → VERIFY → CONVERGE
                     └─ no / resolved
                        ↓
                     REVIEW
               changes ├─ yes → SLICE LOOP (+ DOC FRESHNESS 재실행)
                       └─ no
                        ↓
              INCREMENTAL GARDENING
        repo changed ├─ yes → VERIFY → CONVERGE → DOC FRESHNESS
                     └─ no
                        ↓
                     HANDOFF / DONE
                        ↓
       semantic candidate? → VALIDATE → PROMOTION GATE
```

Documentation Freshness는 REVIEW **앞**이다. reviewer가 보는 시점에 코드와 문서가 이미
맞아야 한다. Gardening은 REVIEW **뒤**다. cleanup이 본 기능을 흔들면 안 된다.

Normal gardening과 compatibility `garden apply`는 report-only다. 사용자 백업과 과거
ADR/incident는 보존한다. 삭제 분류는 승인과 다르다. 실제 정리가 behavior/API/architecture에
영향을 주면 REVIEW도 다시 한다. 검증 실패나 불완전한 scan을 DONE으로 바꾸지 않는다.

Full repository gardening은 `work_type = repository_gardening`인 explicit Work에서만 돈다.
일반 `/work`는 changed scope 주변만 보는 incremental이다.
CLI는 `garden full <gardening-feature> --report-only`로 별도 Work를 요구한다.
문서 split/merge는 `docs preserve --input <request.json>`의 원문 section coverage를
검사한다. exact approval·HEAD·참조·retention·검증된 backup 없이는 retirement하지 않는다.
이 baseline은 destructive CLI를 제공하지 않는다. internal 적용/복구는 trusted adapter와
disposable fixture로 검증하며 실제 저장소 적용 권한을 만들지 않는다.

`BLOCKED`, `DISCOVER`, `AWAITING_APPROVAL`은 정상 상태다. 모르는 지식이나 결정을 코드로 덮지 않는다.

## 2. `/design` state machine

```text
INTAKE
  → REPOSITORY FACTS
  → KNOWLEDGE READINESS / DISCOVERY
  → SHAPE (embedded grill when needed)
  → SPECIFY / CLARIFY
  → ALTERNATIVES / TRADE-OFF
  → PLAN / DECISIONS
  → DOCUMENTATION / DECISION IMPACT (update or supersede)
  → DESIGN READY or AWAITING_APPROVAL
```

`/design`은 code를 구현하지 않으므로 Repository Gardening을 실행하지 않는다. Documentation
Freshness만 적용한다. 중요한 architecture decision이 문서화되지 않은 상태로 DESIGN READY를
선언하지 않는다.

`/design`은 product source를 구현하지 않는다. 저장소에서 확인 가능한 것은 먼저 조사하고 business/architecture decision만 질문한다.

## 3. Knowledge Readiness gate

Domain-heavy/high Work는 다음 8개가 evidence-backed `ready`여야 한다.

```text
terminology / current_behavior / system_boundary / invariants
source_of_truth / contradictions / acceptance / verifier
```

- `unknown` → DISCOVER
- `conflict` 또는 contradiction → BLOCKED
- LOW `tiny_change` → BYPASS 가능
- readiness 미충족 중 implementation 금지

## 4. Context Pack

Context Pack은 Work마다 결정적으로 재생성한다.

```text
required knowledge
active decisions
declared ontology/binding pins when the repository profile provides them
code/test scope
verifier profile
unresolved questions
source hashes + commit
```

`superseded|deprecated|rejected` knowledge는 제외하며 content hash로 drift/tampering을 확인한다.

필수 repository instruction과 적용 가능한 active Decision은 documentation policy의
명시적 governing_inputs 역할로 읽는다. Release guide의 review 여부로 현재 규칙을
누락하지 않으며, 원문 security와 retired-instruction 경계는 유지한다. Historical ledger는
전체 source로 승격하지 않고 active Decision index의 exact ID/path로 선택한다.
Context 검증은 source·claim·Decision의 완전한 집합을 재계산하므로 항목을 뺀 뒤 hash만
갱신한 pack도 거부한다.

## 5. Risk/profile route

- **low**: tiny/local change → `fast`
- **normal**: general feature/bug → `standard`; Context/Environment/Evidence required
- **high**: runtime/public/production impact → registry의 high-risk profile(이 저장소는 `v2`) + applicable gate

실제 변경 경로가 더 위험하면 즉시 상향한다. 자동 하향은 하지 않는다.

## 6. Verification order

```text
computational feedforward guards
→ deterministic feedback verifier
→ semantic/convergence evaluation
→ independent review
```

Verifier result는 실행 environment fingerprint와 함께 남긴다. 실행하지 않은 check는 PASS가 아니다.

## 7. Retry and escalation

같은 failure signature가 기본 3회 반복되면 같은 repair를 멈춘다.

```text
IMPLEMENTATION → minimal repair
TASK           → taskify/slice boundary
PLAN           → plan redesign
SPEC           → clarify/design
REQUIREMENT    → human gate
ENVIRONMENT    → BLOCKED with reproduction requirements
```

## 8. Permission/containment

- Allowed: repo read/edit, test/build/lint, git diff/status
- Gated: architecture/public contract/ontology promotion/destructive/production/git publish
- Prohibited without explicit approval: push/merge/release/deploy/secret disclosure

## 9. Knowledge Evolution (Foundry Profile)

```text
discovery
→ candidate + evidence/provenance
→ existing Proposal validation and diff
→ governed decision / explicit approval gate
```

Ordinary `/work` query는 canonical Vault를 수정하지 않는다. 이 profile에는 RDF/SHACL/CQ
또는 MCP runtime이 없다. Portable 문서 review는 지식 promotion 권한을 만들지 않는다.

## 10. V2 Done

- V1 regression PASS
- readiness/context/environment/evidence/handoff valid
- selected verifier PASS
- converge clean and review PASS
- documentation impact evaluated; canonical docs ACTIVE or intentionally SUPERSEDED/DEPRECATED
- historical decision provenance preserved
- incremental gardening ran on the needed scope
- every garbage candidate carries evidence, risk, and a safety level
- no HUMAN_GATED candidate was deleted automatically
- repository still passes the verifier after gardening
- full gardening only inside an explicit repository_gardening Work
- permission/gate respected
- candidate lifecycle respected
- no new public command beyond /work and /design
- no V3 execution fabric

<!-- AMPLAI-ASYNC-BEGIN -->
## Decision & Async Cross-App Extension

연결 순서:

```text
Goal/Contract
  → Primary Agent
  → Open Question
  → Evidence Resolver (DIRECT|LOCAL|PARALLEL)
  → Decision Resolver (AUTO|CHALLENGE|HUMAN)
  → Implement/Verify
  → Decision/Evidence 기록
  → cross-app Work가 있으면 WAITING/READY dependency loop
```

공용 Project Store의 상태가 세션/agent 대화보다 우선한다. Handoff는 target Work의 projection이며,
Supervisor는 READY/lease/dependency/app capacity만 다룬다. V2에서는 한 앱 한 active Work를 기본으로
하고 global planning, workspace/merge scheduling, publish/deploy는 구현하지 않는다.
<!-- AMPLAI-ASYNC-END -->
