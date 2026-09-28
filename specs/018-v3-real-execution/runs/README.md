# Work 018 — Real Runs (A1, A2)

실제 Codex CLI 0.155.1 (`gpt-5.6-sol`) 를 ContainerSandbox 안에서 돌린 기록이다. 대상은 운영자 clone
`~/.amplai/repos/amplai-foundry` 의 `main` (`575f51d`) 이다. goal 별 JSON 은 로컬 서버의
`GET /api/v3/local/goals/<id>` 응답을 그대로 저장한 것이다 (2026-09-28 수집, credential 문자열 없음 확인).
시각은 UTC 다.

## Outcome

| goal | 요청 | 결과 | 시도 | 소요 | 산출 |
|---|---|---|---|---|---|
| `goal-f43dad1a…` | `amplai ops version` 에 `python_version` 추가 | published | 1 (AC-1..3 pass) | 191.2s | [PR #12](https://github.com/pineskyeo/amplai-foundry/pull/12) draft, +4 lines |
| `goal-b013439a…` | `WorkspaceManager.path` 시험 보강 | published | 1 (AC-1..5 pass) | 189.8s | [PR #13](https://github.com/pineskyeo/amplai-foundry/pull/13) draft, +5/-1 lines |
| `goal-94a177be…` | `성능을 개선해줘` | needs_answers | 0 | — | 질문 2개. 계약·실행 없음 |

두 published goal 모두 base check (수정 전 base 에서 suite 통과) 가 green 이었다. 실행 뒤 운영자 clone 은
branch `operator-wip`, HEAD `575f51d`, untracked `OPERATOR_WIP.txt` 그대로였다 (R9).

## Runs That Exposed Defects

수정 전에 돌린 goal 이다. 실패도 기록으로 남긴다 (A2). 각 행의 수정 commit 은 그 결함을 적은 commit 메시지로
확인했다.

| goal | 요청 시각 | 결과 | 드러난 결함 | 수정 |
|---|---|---|---|---|
| `goal-e251d600…` | 19:01:57 | plan_failed `SCHEMA_INVALID: goal-contract` | model assumption 이 문자열이라 schema object 와 불일치 | `b8d7ef2` |
| `goal-d05567fd…` | 19:04:30 | plan_failed `NODE_VERIFIER` | acceptance 마다 다른 verifier profile — node 는 하나만 가진다 | `ee41968` |
| `goal-07f09db6…` | 19:07:27 | 승인 전 cancelled (19:19:34) | D-073 이전 planner 로 만든 초안. container 안 Codex bwrap 이 shell·쓰기를 모두 실패 | `62e8b52` |
| `goal-fd0029ac…` | 19:12:53 | 승인 전 cancelled (19:19:34) | 위와 같음 | `62e8b52` |
| `goal-8fe0c9d5…` | 19:13:29 | plan_failed `PLANNER_OUTPUT` | 모호한 요청에 acceptance 없이 질문만 온 응답을 거부 | `62e8b52` (같은 문장이 `goal-94a177be…` 에서 needs_answers) |
| `goal-3d1db789…` | 19:19:50 | failed, 3회 모두 AC fail (`unit exited 1`) | `.git` 없는 작업 사본에서 base 자체가 tests/v3 를 통과 못 함 | `7ee99be` |
| `goal-5ea4a8e8…` | 19:20:26 | cancelled, 2회 fail 뒤 3회차 중 취소 | 위와 같음. 취소 전 run 이 `running` 으로 남아 다음 goal claim 을 막음 | `ba5a738`, `2163691` |

## Planner Usage

`planner_usage` 는 Codex 가 보고한 token 수다. 금액은 보고되지 않아 Observatory 에서 cost 는 unknown 이다.
`goal-07f09db6…` 의 input 697,988 token 은 bwrap 실패 상태에서 만든 초안이다.

## Observatory Snapshot

`observatory-2026-09-28.json` 은 metrics 수정 (D-074) 을 반영한 서버의 `GET /api/v3/metrics` 결과다.

- `failure_reasons` 가 두 failure signature 를 `AC-n: "fail: unit exited 1"` 로 풀어 보여 준다.
- `run-572b4b01…` (`goal-5ea4a8e8…` 3회차) 는 `run_state_mismatch` 로 보고된다. end_goal 수정 전에 head 만
  `cancelled` 로 바뀌고 RunRecord 는 `running` 으로 남은 행이다. 값을 고쳐 쓰지 않았다.
- `human_intervention_events` 는 비어 있다. 위 goal 들의 승인은 approval event 가 생기기 전이었다. 과거 대기
  시간은 추정해 채우지 않는다.
