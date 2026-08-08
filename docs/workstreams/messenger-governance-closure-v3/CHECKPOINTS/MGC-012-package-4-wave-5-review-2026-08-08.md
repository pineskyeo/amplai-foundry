# MGC-012 Package 4 Wave 5 Review — 2026-08-08

대상은 MGC-012-T006 (실제 Slack HTTP transport) 와 MGC-012-T007 (credential 경로) 다.
commit `0e8e9bb` 를 round 1 이, 그 수정 `ec3b8fa` 를 round 2 가 봤다. 이 문서는 근거
기록이고 승인 주체가 아니다 (D-004).

**wave 3·4 가 남긴 process 규칙을 이번에는 지켰다.** 읽기만 하는 contract·failure-recovery
두 lens 를 먼저 동시에 돌리고, mutation 을 돌리는 regression lens 는 그 둘이 끝난 뒤
**단독으로** 돌렸다. 세 lens 를 동시에 돌려 재현 불가 실패를 만든 wave 4 와 다르다.

## Round 1 — 읽기 lens 둘

| lens | P0 | P1 | Blocking-P2 |
|---|---|---|---|
| contract | 0 | 3 | 2 |
| failure-recovery | 1 | 2 | 1 |

고유 blocker 는 8건이다. 전부 `ec3b8fa` 에서 닫았다.

### P0 — proxy 의 error 봉투가 되돌릴 수 없는 hold 를 만든다

`_fail_http` 와 `_decode` 가 응답 body 에 문자열 `error` key 만 있으면 **출처를 안 묻고**
`error_code` 를 채웠다. 그러면 allowlist 밖이라 terminal 이고, terminal 은 그 destination 의
이후 Card 를 전부 멈추는 되돌릴 수 없는 hold 를 만든다.

reviewer 가 재현했다.

```text
HTTP 503 {"error": "upstream connect error..."}        → terminal → 영구 hold
HTTP 502 {"error": "Bad Gateway"}                      → terminal → 영구 hold
HTTP 200 {"error": "authentication required"}          → terminal → 영구 hold
HTTP 503 {"ok": false, "error": "service_unavailable"} → retryable  (진짜 Slack)
HTTP 502 {"message": "Bad Gateway"}                    → retryable
```

`classify_slack_failure` 의 설계 전제를 정면으로 깼다. 그 함수 주석이 "code 없는 HTTP
status 는 거의 전부 proxy·WAF 가 낸 것이고 그건 transient 다" 라고 적는데, 새 transport 가
그 code 를 만들어냈다.

**계약도 자기모순이었다.** H-2.1 표는 "body 가 JSON 이면 `error` 도 싣는다" 이고 바로 아래
문단은 "code 를 채우면 되돌릴 수 없는 hold 로 바꾼다" 다. 구현이 표를 따랐다. 판별자는
이미 있었다 — Slack 의 error 는 언제나 `ok: false` 를 달고 오고 중간 장비의 봉투는 안 단다.
`_slack_error_code` 가 그 조건을 요구하도록 고쳤다.

### P1 넷

- **`json.dumps` 와 `Request()` 가 try 밖이었다.** 직렬화 불가 payload 와 scheme 없는
  `base_url` 이 raw 예외로 샜다. C-1 의무 1 이 깨지고 T006 AC-07 이 거짓이었다. try 안으로
  옮겼다. destination 의 `_rewrap` 이 두 번째 방어선으로 있지만 C-1 은 그것을 "대체가
  아니다" 로 못박았고, H-3 의 readback 자가검사는 destination 을 안 거친다.
- **`urlopen(timeout=)` 은 호출 deadline 이 아니다.** socket 연산 하나마다 걸리는 값이라
  상대가 byte 를 찔끔씩 보내면 매 `recv` 가 timer 를 되돌린다. 실측: timeout 1초에 20 byte
  body 가 10.09초. `_read_within` 이 `read1` 로 조각마다 deadline 을 본다 — `read(n)` 은
  Content-Length 만큼 다 받을 때까지 막혀서 검사가 한 바퀴도 못 돈다. 예산 공식도
  `timeout x (pages+1)` 에서 **`2 x timeout x (pages+1)`** 로 고쳤다. 1배는 상한이 아니라
  하한이었다.
- **redirect 가 bot token 을 다른 host 로 보냈다.** stdlib 기본 redirect handler 는
  `content-length` 와 `content-type` 만 떼고 `Authorization` 은 안 뗀다. host 대조도 없다.
  실측으로 다른 host 에 `Bearer` 가 도착하는 것을 확인했다. redirect 를 거부하고
  (`_NoRedirect`) header 도 `add_unredirected_header` 로 단다.
- **`validate_call_budget` 을 production 에서 아무도 안 불렀다.** FR-016 을 닫는다고 선언한
  검증이 아무도 안 부르는 함수로만 있었다. 생성자가 `max_history_pages` 와 `lease_seconds`
  를 **요구**하고 그것을 부르므로 이제 예산을 넘는 transport 를 만들 수조차 없다.

### Blocking-P2 — 근거가 거짓인 guard 를 제거했다

`_RESERVED_PAYLOAD_KEYS` 를 뺐다. 근거로 적은 "payload 가 `metadata` 를 덮어 marker 가 안
나간다" 가 **거짓이었다.** dict literal 은 뒤 key 가 이기므로 그 hazard 는 이 구성에서
발생할 수 없다. 승인 없이 넓힌 계약 표면을 원복하는 것이라 D-024 형식의 결정 기록은
필요 없다. 대신 우리 값이 이긴다는 사실을 test 로 고정했다.

이 guard 는 code 를 비운 채 raise 해서 **결정적 실패를 재시도 경로로** 보내고 있었다.
`max_attempts` 를 전부 소진한 뒤에야 dead letter 에 도달한다.

## Round 2 — regression lens 단독

| P0 | P1 | Blocking-P2 | Advisory |
|---|---|---|---|
| 0 | 0 | 2 | 4 |

mutation 37개를 돌렸다. 1건은 equivalent mutant 로 판명해 제외하고 유효 36건 중
**33 killed, 3 survived** 다. round 1 이 고친 여덟 중 일곱은 teeth 가 확인됐다.

### Blocking-P2 둘 — 둘 다 live bug 가 아니라 test gap 이다

- **`add_unredirected_header` 가 고정되지 않았다.** `add_header` 로 되돌려도 83개가 전부
  초록이었다. 그 줄은 실제로 값이 있다 — reviewer 가 redirect handler 를 기본값으로
  되돌린 상태에서 대조해 `add_header` 면 token 이 가고 `add_unredirected_header` 면 안
  가는 것을 실측했다. 지금은 `_NoRedirect` 가 막고 있어 live bug 가 아니지만, 그 handler 를
  빠뜨리는 다음 commit 이 오면 남은 유일한 방어선이 조용히 사라진다.
- **`_require_text` 의 공백 검사가 고정되지 않았고, 제거하면 raw `ValueError` 가 샌다.**
  `post_message` 의 `SlackSendResult(...)` 구성이 `_call` 의 try **밖**이라
  `SlackSendResult.__post_init__` 의 평범한 `ValueError` 가 그대로 나간다. 즉 그 공백
  검사가 C-1 의무 1 을 지키는 유일한 장치였다. **round 1 의 "raw 예외 유출" fix 가
  미완이었다** — try 안쪽 경로만 덮고 try 바깥 형제 경로를 안 덮었다.

둘 다 test 추가로 닫았고 mutation 으로 죽는 것을 확인했다.

### Advisory 넷 — 셋을 고쳤다

- **`not_a_slack_envelope` 표지가 고정되지 않았다.** 진단 문자열이 아니라 append-only
  column 에 저장되는 code 다 (`exhausted_cause_suffix` 가 `transport_{...}` 로 만든다).
  빠지면 "Slack 이 code 없이 거절" 과 "애초에 Slack 이 아님" 이 같은 row 로 뭉개진다.
  양방향 test 로 고정했다.
- **repo 전면 검사 test 가 CWD 에 의존했다.** 상대 경로 `Path("src/amplai_foundry")` 라
  다른 디렉터리에서 돌리면 0개를 훑고 **조용히 통과**한다. `MODULE_PATH` 기준 절대 경로로
  바꿨다.
- **test 파일 25초 중 90% 가 teardown 대기였다.** `serve_forever` 의 기본 poll interval 이
  0.5초라 test 마다 그만큼 기다렸다. `poll_interval=0.01` 로 바꿨다.
- **`classify_slack_failure` 의 "규칙 순서가 우선순위" 서술은 동작으로 관측되지 않는다.**
  매칭되는 세 규칙이 전부 `RETRYABLE` 을 반환하므로 순열이 전부 같은 동작이다. 429 검사를
  뒤로 내린 mutation 은 equivalent mutant 라 어떤 test 로도 못 죽인다. 결함은 아니다.
  다음 reviewer 가 같은 것을 쫓지 않도록 기록한다.

## Process Finding — Worktree 격리가 이 repo 에서는 기본으로 안 된다

**이번 review 의 가장 값어치 있는 부산물이다.**

regression lens 의 첫 30회 반복이 21/30 실패로 나왔다. 그 수치는 무효다. `git worktree` 를
만들어도 `.venv` 의 editable install `.pth` 가 **main tree 를 가리킨다.**

```text
$ cd <worktree> && .venv/bin/python -c "import ...slack_http as m; print(m.__file__)"
/Users/pinesky/workspace/amplai-foundry/src/amplai_foundry/governance/slack_http.py
```

worktree 의 pytest 가 main tree 의 source 를 import 했고, 그 시점에 lens 는 main tree 에서
mutation 을 돌리고 있었다. 그래서 network 와 무관한 test 까지 깨졌다.

worktree 안에 전용 venv 를 만들어 (`python -m venv .venv-wt && pip install -e .`) import
경로가 worktree 로 풀리는 것을 확인한 뒤 재측정했다.

**wave 5 부터의 규칙에 한 줄을 더한다 — worktree 격리에는 전용 venv 가 필요하다.**
`PYTHONPATH` 를 worktree 의 `src` 로 세우는 것도 같은 효과를 낸다 (wave 4 regression lens 가
그 방식을 썼다).

## Flakiness

새 test 둘이 wall clock 을 assert 한다. **안전하다.**

| 조건 | 실행 | 실패 |
|---|---|---|
| 격리 worktree (전용 venv), 부하 없음 | 30 | **0** |
| CPU 포화 (10 core, burner 20개) | 12 | **0** |

부하를 걸어도 소요가 사실상 안 변한다 — 이 파일의 시간은 CPU 가 아니라 고정 sleep 과 poll
대기가 지배하기 때문이다. 여유도 넉넉하다: dribble test 는 실측 0.61초에 상한 3.0초,
stall test 는 timeout 0.3초에 상한 3.0초다.

`ResourceWarning` 은 안 난다 (`-W error::ResourceWarning` 통과). server 는 전부 정지하고
남은 thread 는 session 종료 시 0 이다.

## Evidence

| command | 결과 |
|---|---|
| `python -m pytest` | **987 passed** |
| `python -m pytest tests/test_slack_http.py` | **89 passed** |
| `python -m ruff check .` | All checks passed |
| `python -m ruff format --check .` | 130 files already formatted |
| `python -m mypy` | Success, 100 source files |
| `amplai-foundry lint vault` | 0 errors, 0 warnings |
| `amplai-foundry verify` | **7/7 PASS** |
| `git status --porcelain src/` | empty — mutation 전부 복원 |

## Verification Of The Real Slack Setup

quickstart A 절을 사용자가 진행했고 실제 Slack 에 확인했다 (`auth.test`, 읽기 전용).

| 항목 | 결과 |
|---|---|
| token | `ok: true` |
| workspace | `amplai` (`T0BNPT96BFD`), enterprise install 아님 |
| granted scopes | **`channels:history, chat:write`** |

scope 가 H-1.1 과 정확히 일치한다. `chat:write.public` 이 없다 — 최소 권한 원칙 그대로다.

**확인 못 한 것 둘.** app 이 internal 인지 (API 로 안 나온다), 그리고 채널 생성·초대
(channel ID 를 몰라 확인 불가).

## Open Item — 설계 구멍 하나 (T009/T010 소유)

검증 중에 발견했다. **T010 이 필요한 값 둘이 갈 곳이 없다.**

| 값 | 왜 필요한가 |
|---|---|
| channel ID (`C...`) | 어느 채널에 Card 를 보낼지 |
| app ID (`A...`) | `reconcile` 이 `app_id` 로 남의 message 를 배제한다. `SlackProjectionDestination` 의 필수 인자다 |

H-4.1 은 변수를 둘로만 정했다 — bot token 과 signing secret. `auth.test` 가 주는
`bot_id`(`B...`)는 `app_id`(`A...`)와 **다른 값**이라 대체할 수 없다. T009 또는 T010 이
계약에 변수를 더해야 한다.

## Verdict

**round 2 종료 시점 P0 0건, P1 0건, Blocking-P2 0건.**

round 2 의 Blocking-P2 둘은 코드 수정 없이 test 추가로 닫았고 mutation 으로 죽는 것을
확인했다. round 2 처리가 test 와 문서만 바꿨고 `src/` 는 `_require_text` 한 줄도 안
바뀌었으므로, wave 2·4 선례를 따라 round 3 을 돌리지 않는다.

gate 기록은 별도 문서다.
