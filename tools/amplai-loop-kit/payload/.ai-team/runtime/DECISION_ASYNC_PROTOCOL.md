---
doc_id: amplai-decision-async-protocol
title: AMPLAI Decision & Async Cross-App Protocol
status: canonical
schema_version: "1.0"
runtime_protocol: amplai.async-cross-app.v1
---

# AMPLAI Decision & Async Cross-App Protocol

이 규약은 여러 앱의 AMPLAI Kit이 같은 변경을 안전하게 이어서 처리하기 위한 공용 계약이다.
각 앱 Agent는 자기 repository 안에서 **어떻게 끝낼지** 판단하고, 공용 Project Store는
**무엇이 결정되었고 누가 다음에 움직일 수 있는지** 보존한다.

## 1. 변하지 않는 경계

- 공개 의미는 `design`, `work`다. Claude Code에서는 `/design`, `/work`; Codex에서는 `$design`, `$work`로 호출한다.
- Question, Evidence, Decision, CR, Work, Handoff는 내부 capability다.
- Handoff Markdown은 SSOT가 아니다. CR·Work·Contract·Decision·Evidence의 현재 projection이다.
- 같은 앱의 다음 세션은 handoff가 아니라 durable Work Context와 session checkpoint로 재개한다.
- Supervisor는 scheduling만 한다. 목표 분해, 설계, 코드 작성, merge, push, deploy, release를 하지 않는다.
- 중앙 Project Store는 `/tmp`가 아니라 Git으로 보존할 고정 경로를 사용한다.

## 2. 중앙 Project Store

```text
$AMPLAI_PROJECT_HOME/
├── project.json                  # Project identity
├── policy.json                   # 공용 authority/evidence/supervisor policy
├── apps/                         # 논리적 App registry (Git 관리)
├── contracts/                    # machine-readable contract reference
├── changes/
│   └── CR-0001/
│       ├── change.json
│       ├── work/
│       ├── questions/
│       ├── decisions/
│       └── evidence/
└── .amplai/
    ├── events.jsonl              # append-only hash chain (Git 관리 가능)
    ├── local/                    # repo 절대경로, command, lease, session, run log
    └── locks/                    # host-local mutation lock
```

`project.json`, `policy.json`, `apps`, `contracts`, `changes`, `events.jsonl`은 공유 상태다.
`.amplai/local`과 `.amplai/locks`는 host-local이며 `.gitignore` 대상이다. 앱 repository에는
`.ai-team/app.json`만 두며 중앙 저장소의 절대경로를 canonical contract로 박지 않는다.
실행 시 `AMPLAI_PROJECT_HOME`을 사용한다.

## 3. 객체 의미

| 객체 | 의미 | SSOT |
|---|---|---|
| CR | 여러 앱이 함께 바꾸는 하나의 변경 | `changes/CR-*/change.json` |
| Work | 한 앱 Runtime이 끝내야 할 실행 단위 | `changes/CR-*/work/*.json` |
| Contract | 앱 사이의 이미 합의된 machine-readable 약속 | `contracts/*.json` |
| Question | Work 중 발견한 아직 닫히지 않은 결정점 | `questions/*.json` |
| Evidence | 코드·테스트·런타임·문서·리뷰·승인 근거 | `evidence/*.json` |
| Decision | Question을 닫은 판단과 authority/provenance | `decisions/*.json` |
| Handoff | target app 관점의 읽기 전용 Work Context | 저장 진실이 아니라 렌더링 |

모든 주요 JSON은 canonical `content_hash`를 가진다. hash가 맞지 않으면 Runtime은 읽기와
실행을 중단한다. 수동 편집은 허용되지만 hash를 다시 기록해야 한다.

```bash
python3 scripts/amplai.py project reseal --path changes/CR-0001/change.json
python3 scripts/amplai.py project reseal --all
```

`reseal`은 ProjectStore를 구성하지 않고 동작한다. `project.json`이나 `policy.json`이 깨져도
복구할 수 있어야 하기 때문이다.

## 4. Decision Runtime

### Authority

`policy.forbidden_automatic_actions` 중 Runtime이 실제로 거부하는 항목은 일부다. 나머지는
worker 프롬프트 지침이며 기계적으로 강제되지 않는다. 무엇이 강제되는지는
`amplai.py project verify`의 `policy_enforcement`가 보고한다.

```text
AUTO
  local + reversible + low impact + evidence-backed

CHALLENGE
  architecture / compatibility / persistence / performance architecture /
  cross-app contract, 또는 low reversibility/high blast radius
  → 독립 reviewer의 ACCEPT evidence 필요

HUMAN
  domain / product / production / safety / security / privacy / legal /
  destructive / public-contract owner decision
  → approved_by가 있는 human_approval evidence 필요
```

상위 authority를 선택할 수는 있지만 policy minimum보다 낮출 수는 없다. Decision은 반드시
Evidence를 가진다. 기존 Decision을 뒤집으면 삭제하지 않고 `supersedes`/`superseded_by`로 연결한다.

### Evidence 조사 방식

```text
DIRECT    이미 확보한 evidence로 판단 가능
LOCAL     Primary Agent가 직접 또는 순차 조사
PARALLEL  서로 독립적인 두 개 이상의 evidence lane만 subagent fan-out
```

Primary Agent가 최종 Decision owner다. Subagent는 evidence worker 또는 independent challenger이며
서로 자유롭게 peer-to-peer 회의하지 않는다. 기본 guardrail은 `max_parallel_lanes=3`,
`max_subagent_depth=1`이다.

## 5. Work 상태기계

```text
DRAFT
  └─ activate ───────────────┐
                             ▼
WAITING ─ dependency DONE ─ READY ─ atomic claim ─ CLAIMED ─ start ─ RUNNING
  ▲                            ▲                                  │
  │                            │                                  ├─ DONE
  │                            │                                  ├─ WAITING
  └──────── dependency ────────┘                                  ├─ BLOCKED
                                                                 ├─ HUMAN_REQUIRED
                                                                 ├─ FAILED
                                                                 └─ CANCELLED
```

- `DRAFT`: `design` 산출물. Supervisor가 실행하지 않는다.
- `WAITING`: 구조화된 dependency가 끝나면 자동으로 `READY`가 된다.
- `BLOCKED`: 자동 해제 조건이 아직 Work dependency로 모델링되지 않았다.
- `HUMAN_REQUIRED`: 사람의 결정/승인이 없이는 자동 재개하지 않는다.
- `FAILED`: retry policy 또는 사람이 재활성화해야 한다.
- `DONE`: acceptance와 evidence를 갖고 종료했다.
- `CANCELLED`: 사람이 포기시켰다. `work cancel` / `change cancel`로만 도달한다.

DONE/CANCELLED handoff는 읽기 전용이다. DONE에는 해당 Work의 summary, 완료 시각,
output과 evidence/decision reference가 표시된다. terminal Work를 재claim하거나 다시
activate하지 않는다. 다음 구현은 별도로 승인된 READY Work에서 이어 간다.
`work heartbeat` CLI는 credential과 원본 lease hash가 없는 detached view를 출력한다.
실제 lease의 token, 갱신, 권한 검사와 retry/backoff는 바뀌지 않는다.

### 막힌 Work 풀기

`_all_dependencies_done`은 `DONE`만 인정한다. 따라서 죽은 upstream은 downstream을 영구히
`WAITING`에 둔다. 다음 세 명령이 유일한 탈출구이며 모두 사람의 명시적 행위다.

```bash
# upstream 을 포기한다. 의존하는 Work 가 있으면 --cascade 없이는 거부한다.
python3 scripts/amplai.py work cancel --id CR-0001-W001 --reason "..." --cascade

# downstream 의 dependency 를 갈아끼운다.
python3 scripts/amplai.py work retarget --id CR-0001-W002 --depends-on CR-0001-W003

# attempt 를 소진한 Work 에 예산을 다시 준다.
python3 scripts/amplai.py work reset-attempts --id CR-0001-W001
```

한 Work가 `HUMAN_REQUIRED`여도 독립적인 다른 앱의 READY Work는 계속 실행된다. 같은 앱은 기본
동시 실행 1개이며 Work claim은 atomic lease로 보호한다. lease가 만료되면 Supervisor가
attempt/max-attempt policy에 따라 READY/WAITING 또는 FAILED로 복구한다.

재시도는 즉시 하지 않는다. 실패하거나 lease를 잃은 Work는 `retry_not_before`가 지나야 다시
claim된다. 간격은 `supervisor.retry_backoff_seconds`에서 시작해 attempt마다 배로 늘고
`retry_backoff_max_seconds`에서 멈춘다.

### 호스트 경계

`changes/`는 Git으로 공유되지만 lease와 lock은 `.amplai/local`, `.amplai/locks`에 있는 호스트
로컬 상태다. 따라서 **atomic claim 보장은 한 호스트 안에서만 성립한다.** 다른 호스트가 claim한
Work는 이 호스트에서 lease가 보이지 않아도 회수하지 않으며, `project verify`가 WARNING으로
보고한다. 여러 호스트에서 동시에 Supervisor를 돌리지 않는다.

## 6. `design`과 `work`

### `design` (Claude `/design`, Codex `$design`)

- Open Question을 구조화한다.
- Evidence 계획과 authority minimum을 정한다.
- cross-app 영향이 확실하면 CR과 앱별 Work를 `DRAFT`로 만든다.
- Contract/acceptance/dependency를 설계하지만 실행하지 않는다.
- HUMAN 결정이 필요하면 질문과 추천안을 남기고 `DESIGN READY` 또는 `awaiting_approval`로 끝낸다.

### `work` (Claude `/work`, Codex `$work`)

- 현재 앱의 READY Work 또는 명시된 DRAFT Work를 가져온다.
- Work Context를 읽고 Evidence를 조사한다.
- AUTO/CHALLENGE/HUMAN 정책으로 Question을 닫는다.
- 구현·검증하고 Evidence를 기록한다.
- 다른 앱 작업이 필요하면 prose-only handoff가 아니라 target Work를 생성·activate하고 현재 Work를
  `WAITING`으로 전환한다.
- 종료 전에 반드시 `DONE|WAITING|BLOCKED|HUMAN_REQUIRED|FAILED` 중 하나를 durable하게 기록한다.

## 7. Handoff와 session continuity

Cross-app 전달:

```text
CR + target Work + Contract + active Decision + upstream result/evidence
                              ↓
                    target-app Handoff View
```

같은 앱의 세션 교체:

```text
Work Context + Project Store + host-local agent session/thread checkpoint
                              ↓
                           resume
```

따라서 handoff 파일이 도착했는지를 polling하지 않는다. Supervisor는 `target_app`, `status=READY`,
`depends_on`, app capacity만 본다.

## 8. Slack Work activation and status projection

Hermes는 Work 요청을 만들 수 있지만 `DRAFT` Work를 activate할 수 없다. AMPLAI Slack App은 signed
button click, actor binding, one-time hash-only token, expected Work revision/digest, 그리고
`project + provider + feature` rollout scope를 확인한 human `activation.manage` authority만 받는다.
기본 scope는 disabled이고 `auto_start=false`다.

Project Store Work는 계속 SSOT다. `scripts/amplai_work_status_projection.py`는 append-only Work event를
local durable outbox에 넣고 Slack status message를 idempotent upsert한다. Slack delivery failure는 pending
delivery로 남을 뿐 Work state를 되돌리지 않는다.

## 9. 미래 Hermes/Global AMPLAI와의 호환성

현재 App Runtime은 host-neutral repository protocol과 Claude/Codex adapter를 통해 Context·Evidence·Decision·Implementation·Verify를 담당한다.
미래 Global AMPLAI는 Goal·CR·Contract coordination·Work Graph·priority·cross-app verification을
담당한다. Global AMPLAI는 앱 내부 구현을 대신하지 않고 동일한 Project Store/Work protocol로
각 App Runtime을 호출한다.
