# 25. 실제 저장소 변경 지도

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. package 이름은 유지

현재 실제 Python package는 `src/amplai_foundry/`다. V3는 제품/아키텍처 세대이며 package를 `amplai`로 무조건 rename하지 않는다. 기존 governance/domain/projects/control_plane/verification 코드를 보호하면서 아래 확장 모듈을 추가한다.

```text
src/amplai_foundry/
  domain/                 existing, no Kit dependency
  governance/             existing authority, preserve guards
  control_plane/          existing API/store + runtime adapter
  projects/               existing registry + AppBinding
  verification/           existing canonical checks + work verdict layer
  runtime/
    contracts/ goals/ graphs/ execution/ budgets/
    effects/ evidence/ storage/ recovery/
  knowledge_runtime/      Foundry read + repo facts + ontology port
  agent_drivers/          typed host/provider adapters
  evaluation/             frozen corpus/experiment/metrics
  meta_harness/           composition + evolution proposals
  distribution/           pinned releases and deployment receipts

packs/                    spec/software/frontend/documents/...
tools/amplai-loop-kit/     portable entries, installer, generated payload
contracts/                normative schemas/registries (versioned)
specs/active/             current implementation definitions
specs/canonical/          approved design summaries
artifacts/evidence/       indexed historical evidence, policy-controlled
```

이는 **제안 목표 구조**이며 지금 생성한 소스 디렉터리가 아니다. 기존 `verification/`를 새 디렉터리로 덮어쓰지 않는다. exact module layout은 기존 imports를 분석한 후 해당 작업의 구현 plan에 고정한다.

## 2. 무조건 삭제하지 않을 자산

legacy_* 현재 importer; Foundry approvals/Decision/ApplyGrant; app override; old canonical ID; referenced evidence; installer baseline/rollback; tests/protected goldens. 삭제 대신 adapter/rename/isolation이 먼저다. 1,669개 metadata inventory를 읽었다는 사실은 모든 파일의 의미 검증을 완료했다는 의미가 아니다.

## 3. 경량화할 표면

18개 일반 Skill entry를 사용자에게 모두 보일 필요는 없다. `/work`,`/design`을 유지하고 internal capability로 내려보낸다. implementation dependency는 줄이되 invariant는 줄이지 않는다. README/AGENTS/CLAUDE는 source of rules가 아닌 progressive map에 가깝게 한다. 규칙 정본은 schema/policy/gate registry이고 entry 파일에는 필수 안전 경계와 위치를 명확히 남긴다.

## 4. 실제 변경 작업서

`migration/component-map.csv`는 source path/prefix, matching file count, action, proposed target, reason, retirement condition을 담는다. `migration/source-disposition.csv`는 normal files 전체의 현재 digest와 분류다. implementer는 해당 작업의 subset을 추출하고 live import/ref scan을 다시 실행한 후 MigrationPlan을 만들며, glob을 그대로 delete command로 변환하지 않는다.
