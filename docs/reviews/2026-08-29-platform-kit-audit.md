# AMPLAI Platform / Loop Kit 2.3.2 Architecture Audit

- Date: 2026-08-29
- Repository baseline: `amplai-foundry` `main` at `ced83af`
- Reviewed products:
  - AMPLAI Platform / Foundry `0.2.0`
  - AMPLAI Loop Kit `2.3.1` → patched `2.3.2`
- Review type: repository structure, host compatibility, runtime safety, product boundary, release/verification readiness

## Executive Conclusion

현재 저장소는 **좋은 핵심 원칙을 가진 강한 prototype**이다. Source 불변성, provenance,
fail-closed apply, Decision authority, Project identity, deterministic verifier, local Work state와
installer rollback 같은 어려운 기반을 이미 구현했다. 단순한 prompt 모음 수준은 아니다.

다만 두 제품이 같은 저장소에 있으면서 역할이 섞여 보였고, Loop Kit은 문서/skill 일부만 Codex에
노출된 상태였지 실제 worker·resume·hook까지 호환되지는 않았다. 이번 2.3.2 patch로 이 단기 결함은
닫았다.

```text
Claude Code / Codex
        ↓ host adapter
AMPLAI Loop Kit          repository-local 개발 실행·검증
        ↓ protocol/reference
AMPLAI Platform          장기 Knowledge/Governance control plane
```

다음 핵심 과제는 기능을 더 붙이는 것이 아니라 **경계를 고정하는 것**이다.

1. Platform과 Kit의 release train 및 의존 방향을 분리한다.
2. Kit의 host 조건문을 명시적 `HostAdapter`로 추출한다.
3. Platform과 Kit 양쪽에 존재하는 Decision/Evidence 모델의 canonical ownership을 정한다.
4. 대형 governance/runtime module을 bounded context로 분해한다.
5. 실제 Claude/Codex foreground smoke와 fleet compatibility matrix가 통과하기 전 unattended
   execution을 켜지 않는다.

## 1. Current Product Boundary

### AMPLAI Platform / Foundry

장기 제품이다. 현재 `src/amplai_foundry/`가 소유하는 것은 다음이다.

- immutable Source ingestion와 duplicate detection
- Knowledge Vault, lifecycle, relation, project identity
- intent classification, Candidate, semantic comparison, Proposal
- Decision authority, audit, outbox, projection, recovery
- Project Pack, roadmap, lint, schema, verification
- Slack ingress/projection을 포함한 external channel adapter

현재 version은 `0.2.0`이고 Python `>=3.11`이다.

### AMPLAI Loop Kit

Platform과 다른 앱을 개발하기 위한 제거 가능한 repository-local runtime이다.

- `work` / `design` controller
- Knowledge Readiness, Context Pack, Contract, verifier, repair/converge/review
- CR/Work/Question/Decision/Evidence를 보존하는 local Git Project Store
- dependency, lease, heartbeat, retry, local Supervisor
- Claude Code/Codex/command worker adapter
- lifecycle hook와 non-destructive installer

현재 version은 `2.3.2`이고 payload는 Python 3.6+ 호환을 목표로 한다.

### Dependency Rule

```text
Allowed
  Loop Kit → Platform repository를 개발·검증
  Platform adapter → 외부 Slack/Git/DB/향후 agent gateway

Forbidden
  Platform domain/application → tools/amplai-loop-kit
  Platform domain/application → .ai-team
  Platform domain/application → Claude/Codex command, session, hook
```

이번 patch 후 `src/amplai_foundry/`에는 Claude/Codex 이름이 남지 않는다. host-specific 표기와
실행 형식은 repository adapter layer에만 있다.

## 2. Findings Closed In 2.3.2

### P0 — Codex Was Documentation-Compatible, Not Runtime-Compatible

기존 저장소에는 `AGENTS.md`와 `.agents/skills`가 있었지만 실제 Supervisor runner는
`claude-code|command`, command construction은 `claude -p`, checkpoint는 Claude JSON,
hook installer는 `.claude/settings.json`만 처리했다.

2.3.2에서 다음을 구현했다.

- native `codex` runner 등록
- `codex exec --json` command construction
- `codex exec resume <thread-id>` continuation
- JSONL `thread.started.thread_id` 회수
- Codex prompt의 `$work` 진입
- Claude/Codex prompt log redaction
- `.codex/hooks.json` SessionStart/SessionEnd additive merge와 uninstall
- Codex용 `hookSpecificOutput.additionalContext`
- Codex sandbox/approval bypass warning

### P0 — Skill Source Of Truth Was Contradictory

문서는 `.claude/skills`를 정본이라고 했지만 Git layout의 다수 skill은 `.agents/skills`를
사실상 정본으로 사용했다. 일부는 복사본, 일부는 symlink여서 두 host workflow가 갈라질 수 있었다.

2.3.2에서는:

- `.agents/skills/`를 유일한 정본으로 고정
- 모든 `.claude/skills/*`를 exact symlink mirror로 통일
- `loopctl doctor`가 skill 집합, visibility, symlink 방향과 깨진 link를 block으로 검사

### P1 — Codex Could Implicitly Bypass The Controller

internal capability도 Codex가 prompt만으로 자동 선택할 수 있었다. 일반 개발 요청이 `$work`의
Knowledge/Contract/Verifier gate를 통과하지 않고 `implement`나 `review`로 바로 들어갈 위험이다.

2.3.2에서는 internal skill의 `agents/openai.yaml`에
`allow_implicit_invocation: false`를 강제하고 doctor regression을 추가했다. 명시적 `$skill`은
가능하지만 암묵적 routing은 controller를 우회하지 않는다.

### P1 — Platform Curation Was Provider-Named

`CurateContextBuilder` 출력과 CLI help가 실제 동작보다 좁은 “Codex” 이름을 사용했다. 기능은
범용 Source/Proposal context였으므로 장기 Platform의 provider independence를 흐렸다.

CLI command와 data contract는 유지하고 bundle title, procedure, help를 agent-neutral로 바꿨다.

### P1 — Codex Hook Operational Trust Was Invisible

Installer가 hook 파일을 만들더라도 project-local hook은 별도 trust review가 필요하다. 2.3.2
installer report와 운영 문서가 `/hooks` review/trust 단계를 명시하고 trust bypass를 자동 추가하지
않는다.

## 3. What Is Already Strong

### Safety And Governance

- Source 원문을 불변으로 보존하고 canonical knowledge 자동 수정을 금지한다.
- Proposal validate/diff/apply gate와 authority context가 분리돼 있다.
- Decision과 evidence, supersede lifecycle, append-only audit/outbox가 있다.
- 실패를 추정으로 메우지 않고 HOLD/fail-closed한다.
- untrusted source를 instruction이 아닌 data로 다루는 규칙이 Context Bundle에 포함돼 있다.

### Loop Reliability

- Work state machine과 dependency가 prose가 아닌 machine state다.
- lease/heartbeat/retry/backoff/attempt budget이 존재한다.
- Supervisor는 의미 판단을 하지 않는 deterministic scheduler다.
- hook/session은 보조 checkpoint이고 Project Store가 SSOT다.
- installer는 checksum, backup, conflict detection, rollback, uninstall을 제공한다.

### Verification Investment

Repository 기준:

```text
src/amplai_foundry       102 Python modules / 약 35,343 lines
tests                    38 test modules / 약 37,625 lines
```

Test code가 product code와 비슷한 규모이며 schema, lint, mypy, Ruff, pytest gate가 이미 있다.
이 기반은 장기 플랫폼으로 가기 좋은 신호다.

### Patch Validation Status

현재 작업 환경은 Python `3.13.5`와 pytest `9.0.2`이며, 저장소 CI 계약인 Python
`3.11/3.12`와 pytest `>=8.3,<9`보다 새롭다. 이 환경에서 전체 suite를 단일 pytest process로
실행하면 테스트가 끝난 뒤 자식 process/global state 정리에서 종료가 멈췄다. 따라서 결과를 숨기지
않고 각 test module을 독립 process로 실행했다.

- 선택된 test: 38 modules, 1,524 tests
- 결과: 권한 의미에 맞는 실행 사용자로 module별 1,524/1,524 통과
- Kit 핵심: async runtime 19/19, regressions 48/48, installer 22/22 통과
- package seal, installer self-test 10개, Loop Doctor 통과
- 수정 Python 파일 `py_compile` 통과
- root 권한이 read-only 의미를 무력화하는 test는 일반 사용자로 재실행해 통과
- package seal을 실제 변경·복원하는 distribution test는 repository 소유 권한으로 통과

실제 `codex`/`claude` CLI binary와 인증은 이 환경에 없어 native foreground smoke와 thread resume는
실행하지 못했다. Ruff와 mypy도 설치되어 있지 않았고 외부 package 설치는 network/DNS 차단으로
불가능했다. 따라서 이 세 항목은 release 전 CI/fleet에서 닫아야 하는 명시적 잔여 gate다.

## 4. Open Strategic Risks

### P0 Before Platform–Kit Integration — Duplicate Decision/Evidence Truth

Platform governance와 Loop Kit Project Store가 각각 Decision/Evidence를 정의한다. 현재는 서로
다른 scope라 동작하지만 향후 연결 시 다음 문제가 생긴다.

- 같은 판단이 두 ID와 두 lifecycle을 가짐
- local engineering decision과 canonical business/architecture decision의 경계가 불명확
- evidence 원본, projection, acceptance 여부가 섞임
- 어느 Store가 supersede authority를 갖는지 충돌

즉시 두 저장소를 합치지 않는다. 먼저 ownership protocol을 정한다.

| Information | Canonical owner | Kit role |
|---|---|---|
| Stable/domain knowledge | Platform Vault | Context Pack에 reference/projected view |
| Business/product/security decision | Platform Decision Ledger | `canonical_ref`로 참조 |
| Local implementation decision | Kit Project Store | 필요할 때 Platform Proposal로 승격 |
| Work/attempt/lease/session | Kit | Platform에는 summary/event reference만 전달 |
| Test/runtime evidence artifact | repo/CI artifact store | Kit가 수집하고 Platform은 accepted reference/provenance 보존 |

권장 protocol field는 `scope`, `origin`, `external_ref`, `canonical_ref`, `promotion_status`다.
이는 runtime protocol revision이므로 2.3.2에 급히 넣지 않고 2.4 설계로 처리한다.

### P1 — Large Modules Hide Boundaries

측정된 주요 파일:

```text
src/amplai_foundry/governance/migrations.py             5,186 lines
src/amplai_foundry/governance/events.py                 3,494 lines
src/amplai_foundry/governance/legacy_migration.py       2,705 lines
tools/amplai-loop-kit/.../amplai_runtime.py             2,432 lines
scripts/loopv2.py                                       2,119 lines
scripts/loopctl.py                                      1,142 lines
```

크기 자체보다 서로 다른 변경 이유가 한 파일에 모이는 것이 문제다. 작은 feature도 broad regression과
review context를 요구하고 host 또는 storage 변경이 domain logic을 흔들 수 있다.

권장 분해:

```text
Platform
  domain/             pure model, invariant, lifecycle
  application/        use cases, commands, queries
  ports/              repository, event bus, identity, channel
  adapters/           sqlite, filesystem, git, slack
  migrations/         immutable versioned migration units + runner
  projections/        replayable read models

Loop Kit
  domain/             Work/Question/Decision/Evidence state rules
  store/              Project Store and locking
  supervisor/         claim/reconcile/process lifecycle
  hosts/base.py       HostAdapter contract
  hosts/claude.py
  hosts/codex.py
  hosts/command.py
  hooks/               host hook input/output adapters
```

### P1 — Payload And Installed Copy Can Drift

Kit 정본은 `tools/amplai-loop-kit/payload/`, 현재 저장소의 실행 사본은 `scripts/`와 `tests/ai/`에
있다. checksum과 installer가 방어하지만 사람이 두 곳을 직접 편집하면 review diff가 복잡해진다.

개선 원칙:

1. 정본은 payload만 직접 수정한다.
2. root installed copy는 installer로만 생성한다.
3. CI에서 clean temporary target install 후 payload와 installed hashes를 비교한다.
4. `git diff`에 root copy만 바뀌거나 payload만 바뀌면 fail한다.

### P1 — Claimed Python Compatibility Has No Separate Matrix

Platform CI는 Python 3.11/3.12를 검사한다. Kit payload는 Python 3.6+를 목표로 하지만 product와
같은 workflow 안에서만 간접 검증된다. Python 3.6 호환을 계속 계약으로 유지할 것이면 별도
container/matrix가 필요하다. 유지 비용이 더 크면 2.4에서 실제 fleet 최소 version으로 상향하고
명시적으로 breaking change를 낸다.

### P1 — Native Host Contract Needs Real Smoke Evidence

Deterministic tests로 command shape, parser, hook output을 검증할 수 있지만 실제 CLI 설치/auth,
project trust, sandbox/approval, session resume까지 증명하지는 못한다.

Fleet `auto_start` 전 host별 최소 시나리오:

```text
1. READY Work를 foreground로 claim
2. 한 파일을 허용 scope에서 수정
3. verifier evidence 기록
4. terminal state DONE 기록
5. 같은 thread/session으로 follow-up resume
6. prompt/token/secret가 run log에 없는지 검사
7. hook trust와 SessionStart context 확인
```

### P2 — Helper Skill Surface Is Wider Than The Main Product Path

개발 controller는 `work`/`design` 둘로 명확하지만 `eli12`, `feynman`, `grill-me`, `grilling`이
동시에 공개돼 있다. 설명·질문 보조라 loop를 직접 깨지는 않지만 이름과 목적이 겹치고 host별
selector가 복잡해진다.

권장: 2~4주 사용 telemetry 또는 수동 기록 후 하나의 `explain` 계열과 하나의 `grill` 계열만
남긴다. 특히 어린이식 표현으로 오해되는 skill은 제거하거나 accessible explanation 정책으로
흡수한다.

## 5. Target Architecture

```text
                    ┌──────────────────────────────┐
                    │ Hermes / UI / Operator API   │
                    └──────────────┬───────────────┘
                                   │ commands / approvals
                    ┌──────────────▼───────────────┐
                    │ AMPLAI Platform Control Plane│
                    │ Knowledge · Governance       │
                    │ Decision · Audit · Identity  │
                    └──────────────┬───────────────┘
                      Context refs │ Evidence/Decision refs
                    ┌──────────────▼───────────────┐
                    │ AMPLAI Kit per repository    │
                    │ Contract · Work · Verify     │
                    │ Local Supervisor             │
                    └───────┬───────────┬──────────┘
                            │           │
                      ClaudeAdapter  CodexAdapter
                            │           │
                       Claude Code     Codex
```

Platform은 agent를 직접 조종하는 세션 manager가 아니라 **정책과 canonical state의 control
plane**이 된다. Kit은 repository 안에서 실제 개발 loop를 수행하는 **execution client**다.
Hermes는 두 층을 대체하지 않고 사용자 interaction과 cross-app coordination을 담당한다.

## 6. Recommended Roadmap

### Now — Release 2.3.2 Safely

현재 repository-level deterministic validation은 완료됐지만 native host 및 지원 toolchain gate는
아직 남아 있다.

- Python 3.11/3.12 + pytest 8 CI에서 full suite를 한 번에 통과
- Ruff, mypy, repository verifier 통과
- 실제 Codex foreground smoke와 resume 1건
- 실제 Claude Code foreground smoke와 resume 1건
- `/hooks`에서 project-local hook review/trust 확인
- clean temporary target install 후 payload/installed hash parity 확인
- amplai-foundry → synapse → cortex 순서로 dry-run 후 배포
- 각 repository에서 local modifications와 baseline failure를 별도 기록
- 위 gate 전까지 `auto_start=false` 유지

### 2.4 — Host Adapter And Protocol Hygiene

- `HostAdapter` interface 도입: build command, redact command, parse continuation id, hook output
- Claude/Codex 조건문을 Supervisor/Hook 본문에서 제거
- Decision/Evidence federation fields와 promotion protocol 설계
- payload → installed copy reproducibility CI
- supported Python matrix를 실제 fleet 기준으로 확정
- helper skill surface 정리

### Platform 0.3 — Modular Governance Core

- `events.py`, `migrations.py`, legacy migration을 bounded module로 분해
- command/query와 SQLite/filesystem/Slack adapter 분리
- event envelope와 schema version, idempotency key, causation/correlation id 고정
- replay test와 projection rebuild test를 release gate로 추가
- one-shot legacy migration은 active application package에서 분리해 immutable migration artifact로 보존

### Platform 0.4 — Service Boundary

V2 local evidence가 안정된 뒤에만 진행한다.

- authenticated API와 Project/Tenant authorization
- durable job worker와 outbox delivery
- connector ingestion boundary와 secret isolation
- Context Request / Evidence Publish / Decision Reference API
- UI는 이 API의 projection client로 구현하고 canonical write authority를 갖지 않음

### Later — Distributed V3

다음 조건 전에는 distributed multi-agent fabric을 시작하지 않는다.

- single-repo Work terminal transition이 신뢰 가능
- duplicate Decision/Evidence ownership이 해결됨
- retry/lease/session recovery metrics가 수집됨
- cross-app contract round trip의 실패 유형이 분류됨
- merge/workspace/resource scheduling이 별도 protocol로 설계됨

## 7. Release Gates

### Platform Gate

- domain/application layer에 provider/Kit import 0건
- direct canonical mutation 0건
- every accepted Decision has authority + evidence + audit event
- event replay로 projection을 동일하게 재구성
- schema/vault/project pack drift 0건
- rollback/recovery test 통과

### Loop Kit Gate

- Claude/Codex/command adapter contract tests 통과
- `.agents/skills` canonical 및 `.claude` exact mirror
- internal Codex implicit invocation disabled
- installer fresh/update/idempotent/uninstall/rollback 통과
- existing Claude/Codex hooks 보존
- SessionEnd timeout <= 3s
- prompt/lease token redaction 확인
- unattended bypass warning 0건 또는 명시적 human acceptance
- enabled native host별 foreground smoke evidence

## 8. Do Not Do Next

- Platform version과 Kit version을 하나로 합치지 않는다.
- Claude용 Loop와 Codex용 Loop를 복사해 두 벌로 만들지 않는다.
- Kit Project Store와 Platform Governance Store를 schema 검토 없이 합치지 않는다.
- `amplai_runtime.py`에 세 번째 provider 조건문을 계속 추가하지 않는다.
- V2가 측정되기 전에 V3 parallel agents, distributed lease, auto-merge를 켜지 않는다.
- Hook 성공을 durable Work 완료로 간주하지 않는다.
- 편의를 위해 sandbox/approval/hook trust를 installer가 자동 우회하지 않는다.

## Final Assessment

- **Platform 방향:** 맞다. 현재는 “Knowledge Foundry + Governance foundation”으로 정의하는 것이
  정확하며, 아직 범용 AI platform server라고 부르기에는 API/identity/runtime integration이 덜
  닫혔다.
- **Loop Kit 방향:** 맞다. Platform 내부에 흡수하지 말고 독립 development runtime으로 유지해야
  한다. 2.3.2 patch로 Claude/Codex 공통 protocol의 형태가 갖춰졌다.
- **가장 중요한 다음 설계:** 새로운 agent 기능이 아니라
  `Platform canonical Decision/Evidence ↔ Kit local Work/Evidence` federation contract다.
