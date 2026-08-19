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

Gardening이 SAFE_AUTO generated garbage만 지웠으면 REVIEW를 다시 요구하지 않는다.
behavior/API/architecture에 영향을 주는 변경이면 REVIEW도 다시 한다.

Full repository gardening은 `work_type = repository_gardening`인 explicit Work에서만 돈다.
일반 `/work`는 changed scope 주변만 보는 incremental이다.

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
active ontology neighborhood
code/test scope
verifier profile
unresolved questions
source hashes + commit
```

`superseded|deprecated|rejected` knowledge는 제외하며 content hash로 drift/tampering을 확인한다.

## 5. Risk/profile route

- **low**: tiny/local change → `fast`
- **normal**: general feature/bug → `standard`; Context/Environment/Evidence required
- **high**: runtime/semantic/public/production impact → `v2|semantic|full|rhel` + applicable gate

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

## 9. Semantic evolution

```text
discovery
→ candidate + evidence/provenance
→ RDF parse
→ SHACL fixtures
→ competency regression
→ consistency/collision + semantic diff
→ explicit promote/reject
```

Ordinary `/work` query는 canonical graph를 수정하지 않는다. MCP surface는 read-only다.

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
