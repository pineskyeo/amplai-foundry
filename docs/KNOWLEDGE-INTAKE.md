# Knowledge Intake Contract

Phase 1 Intake의 사용자 계약은 “의도 + 파일”이다. 사용자는 저장 폴더, ID, 지식 kind나 relation을 지정하지 않는다.

```text
IntentRequest
→ ProjectResolver
→ immutable Source
→ ArtifactClassification
→ KnowledgeCandidate
→ SemanticComparator
→ PolicyDecision
→ review Proposal
→ ValidationReport + MinimalEvaluationRecord
```

## Safety Boundary

- Project는 Source 등록 전에 확정한다. 모호하면 Source/Canonical은 쓰지 않고,
  workspace-level `ResolutionHoldRecord`만 저장한다.
- Source는 UTF-8 원문 bytes, exact/normalized SHA-256, origin과 authority를 보존한다.
- 분류에는 최소 두 개의 독립 signal이 필요하다. 동점 또는 약한 signal은 `UNKNOWN/HOLD`다.
- Candidate는 공식 지식이 아니다.
- `UNCERTAIN`과 `CONFLICTS`는 항상 `HOLD` 또는 review다.
- `NEW`와 `REFINES`는 Proposal만 만들며 canonical apply를 수행하지 않는다.
- Public `IntentRequest`는 external identity fact만 받는다. Permission과
  `AuthorityContext` 입력은 거부한다.
- `AuthorityService`는 Source 등록 전에 active Actor binding과 Project permission을
  다시 확인한다.
- Intake Policy Actor는 `proposal.read`, `proposal.submit_review`만 가진다.
- Duplicate evidence와 Tracker status도 Proposal로 남기며 자동 적용하지 않는다.
- 공식 decision은 `DecisionService`를 통과한다. Apply는 `MGC-009` 전까지 deferred다.
- 동일 project, instruction, source hash는 같은 IntakeRun ID와 Proposal을 재사용한다.
- Intake 전후에 Project Pack, Vault lint, typed artifact와 Proposal 계약을 검증한다.

## Minimal Evaluation Record

각 run은 artifact-kind별 fixture ID와 함께 project resolution, classification, duplicate
여부, safe/unsafe apply 여부, reproducibility, correction 수, latency, token usage와
failure category를 저장한다. 프로젝트를 확정하지 못한 요청도 fingerprint만 가진 hold
record로 남는다. Phase 8 Observatory는 이 기록을 replay baseline으로 사용한다.

```bash
amplai-foundry intake process roadmap.md \
  --instruction "이 로드맵을 AMPLAI에 반영해줘" \
  --project amplai \
  --provider-installation local:cli \
  --external-actor-id ACTOR-BINDING-KEY \
  --json
```

여러 artifact는 한 요청에서 같은 Project resolution을 공유하지만 Source, Candidate, 비교, Proposal과 IntakeRun은 artifact별로 분리된다.
