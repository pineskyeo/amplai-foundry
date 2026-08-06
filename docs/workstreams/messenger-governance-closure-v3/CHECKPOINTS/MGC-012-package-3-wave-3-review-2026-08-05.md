# MGC-012 Package 3 Wave 3 Review — 2026-08-05

대상은 MGC-012-T003 (`SlackProjectionDestination.reconcile`) 다. reviewer 셋을 관점별로
동시에 돌렸다 — contract, failure/recovery, regression. 이 문서는 근거 기록이고 승인 주체가
아니다 (D-004).

## Round 1

### 먼저 — failure lens 의 P0 는 실재하지 않는다

failure lens 가 `_never_attempted` 의 본체가 뒤집혀 있다고 P0 로 보고했다. **그 상태는
실재한 적이 없다.** 세 reviewer 를 동시에 돌렸고 regression lens 가 mutation testing 으로
같은 파일을 계속 바꾸는 중이었다. failure lens 가 읽은 것은 그 mutation (M04) 이다. 해당
보고서도 "working tree changed under me three times" 로 그 사실을 스스로 적었고, regression
lens 는 SHA-256 대조로 원복을 증명했다. 검토 후 확인한 파일의 sha256 은 regression lens 가
기록한 복원 값과 같고 본체는 `attempts <= 1 and last_error_code is None` 이다.

**교훈**: mutation 을 돌리는 lens 와 코드를 읽는 lens 를 동시에 돌리면 안 된다. 다음
wave 부터 regression lens 를 따로 돌린다.

### Gate

**round 2 에서 세 lens 전부 P0·P1·Blocking-P2 가 0건이다.** round 1 이 남긴 두 가지도 닫혔다.

1. **`app_id` 의 C-2 추가** — D-024 항목 1 로 기록했다. 절차 지적을 그 Decision 안에
   그대로 적었다. T002 의 D-021 과 같은 자리다.
2. **history 소진과 "marker 를 못 읽는 상태" 의 구분** — D-024 항목 2 의 지문으로
   `include_all_metadata` 누락과 `app_id` 누락을 잡는다. **전부는 못 잡는다** — 형식은
   맞지만 값이 틀린 `app_id`, `metadata` 를 통째로 안 옮기는 adapter, `event_type` 개명은
   여전히 조용한 중복 Card 다. 그것들은 destination 안에서 판정할 수 없어 C-1.2 의
   **readback 자가검사**를 구현체 의무로 추가했다 — adapter 가 기동 전에 자기가 보낸
   message 를 되읽어 marker 가 복원되는지 확인하고 실패하면 기동을 거부한다. 셋을 한 번에
   잡는 유일한 검사이고 Package 4 의 exit criteria 다.

### 남은 위험 — 수용하고 기록한다

- **page 사이의 정렬**은 여전히 계약 의존이다. cursor 를 우리가 만들지 않아 destination
  안에서 막을 수 없다. 위반이 조용하고 결과가 회수 불가라 C-1 의 note 는 완화가 아니라
  기록이다. Package 4 가 Slack 문서로 확정한다
- **`app_id` 지문의 오탐 하나** — 제3의 app 이 우리 `event_type` 을 `app_id` 없이 쓰면
  되돌릴 수 없는 hold 가 된다. Slack 이 `app_id` 를 항상 붙이는지 확인 못 했다. 방향
  때문에 수용했다 (조용한 중복 vs 시끄러운 hold)
- `conversations.history` OAuth scope 가 C-1.2 에 없다. `missing_scope` 는 terminal 이라
  첫 재시도에서 hold 가 된다. Package 4
- `events.py:2843`-`2853` 의 guard 가 미전송이 증명된 Card 를 버릴 수 있다. C-3.1 이 수용
- `max_attempts` 과소 배선의 trigger 가 read 실패로 넓어졌다. Package 4 의 `OutboxConfig` 배선
- M21·M22 (`read_slack_marker` 의 개별 str guard) 가 mutation 에서 살아남는다. 잘못된 type 의
  `SlackMarkerView` 는 모든 비교에 실패하므로 실해는 낮다

### 판정

**PASS 로 열 수 있다.** 근거는 셋이다.

- round 2 의 세 lens 전부 blocker 0건
- round 2 처리에서 바뀐 production code (`app_id` 지문, `event_payload is None`, 검사 순서)
  는 regression lens 가 **그 뒤에** 단독으로 mutation 44개를 돌려 전부 잡히는 것을 확인했다.
  round 1·2 와 달리 "수정이 미검토로 남는" 상태가 아니다
- 그 이후의 변경은 주석·문서뿐이다 (근거 없는 단정 제거와 오탐 명시)

명령 결과는 위 Commands 절이다. 전부 실행했다.

Decision 은 D-023 (첫 전달 규칙) 과 D-024 (app_id, 조회 결함 지문, 정렬 의무, digest
불일치, lease 예산) 둘이다. 이 문서는 근거이고 승인 주체가 아니다 (D-004).
