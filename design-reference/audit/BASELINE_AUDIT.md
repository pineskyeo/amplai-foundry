# 업로드 기준선 실측 감사

원본: `amplai-foundry.tar.gz` · SHA-256 `1a9b8e656b5a133087c20d8164dd7ff6276c34d7e74cff59223f71e9ece954ef`

## 검사 범위
tar 21,476 entries 중 regular 20,540개. 환경·캐시·Git 내부를 제외한 normal file 1,669개를 inventory/hash로 기록했고, 핵심 runtime/governance/contracts/host/roadmap을 정적으로 확인했다. **전체 파일의 의미를 모두 검증했다거나 기존 tests를 실행했다는 뜻은 아니다.** 원본은 변경하지 않았다.

symlink/hardlink는 별도 archive metadata로만 파악하고 normal-file extraction에 포함하지 않았다. `.claude/skills` 같은 mirror의 부재는 이 방식만으로 단정하지 않는다. `.git`·가상환경·runtime secret 원문은 패키지에 복사하지 않았다.

## 실제 확인과 정정
- **BASE-01** `README.md:7` — Platform/Kit 독립 release train과 domain→Kit 의존 금지. 유지한다.
- **BASE-02** `pyproject.toml:10` — Platform Python >=3.11. CP 기본값과 old client 경계를 분리한다.
- **BASE-03** `README.md:12` — README는 Kit2.5.0을 로컬 후보로 말한다. fleet release 검증으로 해석하면 안 된다.
- **BASE-04** `tools/amplai-loop-kit/VERSION:1` — VERSION은2.4.0. README candidate와 release truth reconciliation 필요.
- **BASE-05** `scripts/amplai_runtime.py:1240` — Work dependency와 cycle validator가 이미 존재. 새 generic graph framework를 필수로 넣지 않는다.
- **BASE-06** `scripts/amplai_orchestration_bridge.py:47` — target hint 없음에 HOLD. 사용자 hint 의무는 resolver로 대체하되 resolved target 의무 유지.
- **BASE-07** `tools/amplai-loop-kit/manifest.json:56` — 인접 marker는 required:false,create_if_missing:false. 미설치만으로 manifest 결함 확정 불가.
- **BASE-08** `src/amplai_foundry/governance/publish_resolution.py:22` — 현재 publish path가 legacy guard를 import한다. 무조건 삭제 불가.
- **BASE-09** `src/amplai_foundry/governance/store.py:32,36,39` — store가 legacy implementation을 동적으로 연결한다. behavior equivalence 후 대체한다.
- **BASE-10** `.ai-team/contracts/README.md:20` — 계약 README는 repository_gardening work_type을 포함한다.
- **BASE-11** `scripts/loopv2.py:1931` — 실제 gardening guard도 repository_gardening를 요구한다. 현재 schema enum과 drift 점검.
- **BASE-12** `plans/amplai-master-roadmap.yaml:5` — 로드맵의 갱신일과 현재 module presence가 다르다. 실제 검증상태로 rebaseline한다.
- **BASE-13** `src/amplai_foundry/control_plane/store.py:117,136,137` — Control plane outbox/storage 자산을 재사용·연장할 수 있다.
- **BASE-14** `scripts/amplai_hosts.py:106` — host abstraction 시작점이 존재. typed provider decoder/qualification을 강화한다.
- **BASE-15** `scripts/amplai_hosts.py:147` — Codex host 경계를 모델 이름과 분리한다.
- **BASE-16** `.ai-team/runtime/WORKFLOW.md:12,14,70` — 기존 readiness·context·bounded verify·gardening 원칙을 adaptive gate로 재구성한다.

계약 schema 내 `repository_gardening` 부재 확인: **True**. README/runtime 요구와 대조해야 할 실제 정합성 항목이다.

일반 파일로 확인한 public Skill entry 18개: code-review, design, dev-loop, eli12, grill-me, grilling, speckit-analyze, speckit-checklist, speckit-clarify, speckit-constitution, speckit-converge, speckit-implement, speckit-plan, speckit-specify, speckit-taskstoissues, systematic-debugging, taskify, work.

## 데이터 부피와 삭제 원칙
| 경로 | normal files | bytes |
|---|---:|---:|
| specs | 916 | 10,442,945 |
| src | 121 | 1,696,840 |
| tools | 120 | 1,162,569 |
| .ai-team | 93 | 2,326,455 |
| .amplai | 77 | 258,754 |
| .agents | 60 | 716,145 |
| docs | 67 | 646,077 |
| tests | 78 | 1,886,513 |
| vault | 57 | 146,176 |

큰 폴더라고 불필요한 것은 아니다. evidence는 retention/ref/gate를 적용해 cold archive로 옮기고, 승인·rollback 관련 legacy 코드는 대체 후 equivalence test를 통과해야 삭제 후보가 된다. `source-disposition.csv`는 계획이지 삭제 명령이 아니다.

## 실행하지 않은 것
기존 unit/integration test, 실제 AMPLAI V3 runtime, 사내 DB migration, Claude/Codex/OpenCode 실제 API, RHEL 호환성, 보안 침투시험, production 배포는 실행하지 않았다. 이번 수행은 설계 패키지의 구조·참조·schema/fixture validation이다.
