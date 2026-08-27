# Three-Lens Review — Round 19 (MGC-012-P5 Wave 12)

**판정: FAIL.** blocker 9건 — P0 0 / P1 1 / Blocking-P2 8. Advisory 14.

Target: `evidence/review-target-round-19.txt`, 85 파일, aggregate
`2cff6e1b4dcc379d76d7fe137d910becbae8576168178602f636556418d4c417`.
세 reviewer 가 착수·종료에 **행별 hash 재계산**으로 확인했고 셋 다 **어긋남 0건**이다.
`T040` 이 넣은 aggregate 한계 문구가 작동했다 — 세 reviewer 전부 aggregate 를 근거로 쓰지
않고 그 사실을 명시했다. round 18 에서는 셋 다 혼동했다.

**round 18 대비 5건 → 9건. 늘었다.** 등급은 유지됐다 (P0 0, P1 1).

## Round 18 Blockers: Two Closed, Three Partial

| round 18 | 등급 | 판정 | 확인 방법 |
|---|---|---|---|
| `N18-1` | P1 | **부분** | `WHERE state != 'completed'` 실재. mutation M11 이 KILLED. **다만 `completed` 외 다른 벽에서 같은 signature 가 재현된다 — `F19-1`** |
| `N18-2` | B-P2 | **부분** | 새 경로가 docstring 과 표에 실재하고 test 로 고정됐다. **다만 우회 경로가 둘인데 표는 하나만 적었다 — `F19-2`** |
| `N18-3` | B-P2 | **부분** | census 를 두 방법으로 재계수했다. **분류표가 26 파일 중 24개만 댄다 — `C19-4`** |
| `N18-4` | B-P2 | **부분** | 재확인 script 어긋남 0, aggregate 한계 문구 실재, Stop rule 셋 실재. **다만 `T040.md` 의 최종 freeze 절이 비어 있다 — `C19-2`** |
| `F18-R1` | B-P2 | **닫힘** | mutation M12 가 KILLED, `..._a_sweep_failure_alone_still_closes_the_claim` 1건. `M11` 과 다른 test 다 |
| `A18-2` | Adv | **닫힘** | `T033.md` 의 14 인용을 reviewer 가 AST 로 독립 재계수해 전부 일치 |

## New Blockers

| id | lens | 등급 | 위치 | 결함 |
|---|---|---|---|---|
| `F19-1` | failure | **P1** | `ingress.py:644`, `:626-635`, `cli.py:743-744`, `D-048` | **`D-048` 이 `completed` 하나만 뺐다.** `unreadable()` 의 후보 집합 `{pending, leased, retry_wait, recovery_hold, dead_letter}` 와 `_stranded_rows` 의 `{dead_letter, recovery_hold, retry_wait&소진, leased&소진&만료}` 가 **다르다.** docstring 의 "`limit` 은 `stranded()` 와 같은 뜻이다" 가 거짓이다. `pending`·`retry_wait(미소진)`·`dead_letter`·`recovery_hold` 벽이 `N18-1` 과 **같은 signature** 를 낸다. `dead_letter`·`recovery_hold` 도 벗어나는 UPDATE/DELETE 가 없어 영구히 쌓인다 |
| `C19-1` | contract | B-P2 | `work-contract.json:47` | AC-002 가 `INGRESS_COMMAND_UNREADABLE` 을 요구하는데 구현은 `INGRESS_LEASE_EXPIRED` 를 남기고 test 가 그것을 단언한다. wave 12 가 "다른 경로" 로 판단하고 그 판단을 `T038.yaml` AC-03 으로 **조용히 완화**했다. contract 원문에 정정도 각주도 없고 `D-045` 에도 재해석이 없다. **`all_acceptance_passed: true` 로 닫혔다** |
| `C19-2` | contract | B-P2 | `evidence/MGC-012-P5-T040.md:165` | **`### 두 번째 freeze — 최종` 제목만 남기고 파일이 끝난다.** 바로 앞 문장이 "아래가 그 결과다" 인데 아래에 아무것도 없다. 남은 유일한 재확인 출력은 **첫 freeze** 것이고 그 aggregate(`fc1af0eb…`)는 실제 얼린 값(`2cff6e1b…`)과 다르다. `T040.yaml` 이 그 출력을 `required: true` 로 걸었는데 `required_evidence_present: true` 로 닫혔다 |
| `C19-3` | contract | B-P2 | `ingress.py:610` | `unreadable()` docstring **요약 줄**이 아직 `List every durable row that cannot be turned into a view.` 다. `D-048` 이 승인한 범위와 **반대를 단언한다.** 형제 셋(`stranded()`·`get()`·`is_unreadable()`)의 요약 줄은 정확하다 |
| `C19-4` | contract | B-P2 | `evidence/MGC-012-P5-T039.md:44-56` | census 가 "79줄 / 26파일" 을 얻고 분류표를 냈는데 **표가 댄 파일은 24개**다. 빠진 둘은 `T018.md:36`, `T019.md:23` 이고 둘 다 다른 맥락이라 `다른 맥락` 행에 들어가야 했다. **`N18-3` 과 정확히 같은 형태다** — "전수" 라 쓰고 표는 부분이다 |
| `F19-2` | failure | B-P2 | `ingress.py:535-543` | "도달하지 않는 경로가 **하나** 있다" 가 틀렸다. **둘**이다. 둘째는 `retry_wait`+소진+`retry_at <= now` 로, 그 경로는 `last_error_code` 를 **안 덮으므로** 앞선 값이 남거나 `NULL` 이다. `NULL` 이면 CLI 가 `dead_letter … -` 를 낸다 |
| `R19-1` | regression | B-P2 | `ingress.py:652-655` | `unreadable()` 의 `except _UNREADABLE_ROW` 를 `except Exception` 으로 넓혀도 **1392개가 다 통과한다.** AC-06 표가 이 칸에 적은 test 는 `get()`·`stranded()` 만 친다. 결과 — `_view` 안의 구현 결함이 operator 에게 "손상됐다" 로 보고된다. round 15 `F-2` 가 `stranded()` 에 대해 막은 거짓 양성이 새 조회에서 열려 있다 |
| `R19-2` | regression | B-P2 | `ingress.py:646` | `ORDER BY received_at, command_id` 를 `ORDER BY command_id DESC` 로 바꿔도 1392개가 다 통과한다. 표는 "두 test 가 순서에 의존" 이라 적지만 **둘 다 의존하지 않는다.** `F19-1` 이 열려 있는 한 정렬이 **어떤 손상 row 가 `limit` 안에 드는지**를 정한다 |
| `R19-3` | regression | B-P2 | `evidence/MGC-012-P5-T038.md:48-54` | AC-06 합계 "test 가 있는 것 **12**" 가 실측 **10** 이다. 구조 1 이 6 이 아니라 4 다. **이 표가 다음 라운드의 blocker 목록을 뽑는 근거**인데 수치가 틀리면 `R19-1`·`R19-2` 가 "이미 막았다" 로 분류돼 영구히 안 잡힌다 |

## The Root — 덜 세는 것이 메타 레벨로 올라갔다

round 16·18 의 뿌리는 "새 구조가 요구하는 것을 덜 셌다" 였다. wave 12 는 그것을 고치려고
**요구를 세는 표**(`T038` AC-06)를 만들었다.

**그 표 자체가 덜 셌다.** 12칸이 "test 있음" 인데 실측은 10 이고, 틀린 두 칸이 각각
`R19-1`·`R19-2` 가 됐다.

**빈 칸보다 나쁘다.** 빈 칸은 `non_goals` 로 명시적으로 뺀 것이라 다음 라운드가 본다.
채워졌다고 적힌 칸은 "이미 막았다" 로 분류돼 아무도 안 본다.

같은 형태가 세 곳에서 반복된다.

| 무엇을 세나 | 주장 | 실측 |
|---|---|---|
| `unreadable()` 이 빼야 할 state (`D-048`) | `completed` 하나 | 후보 집합이 `stranded()` 와 다르다 — 넷 더 (`F19-1`) |
| `_dead_letter_unreadable` 에 도달 안 하는 경로 | 하나 | **둘** (`F19-2`) |
| census 분류표가 댄 파일 | 26 | **24** (`C19-4`) |
| AC-06 의 test 있는 칸 | 12 | **10** (`R19-3`) |

## Verified Clean

- **mutation M11·M12·M13 전부 KILLED.** 두 lens 가 독립으로 재현했고 죽은 test 이름과
  건수까지 `T038` 주장과 일치한다. **`.pyc` 위조 없이** 재현됐다 — `T040` 이 넣은 절차가
  작동했다.
- **`T040` 의 aggregate 한계 문구가 작동했다.** 세 reviewer 전부 aggregate 를 무손상 근거로
  쓰지 않고 행별 재계산 결과를 인용했다. round 18 에서는 셋 다 혼동했다.
- **freeze 재확인 script 어긋남 0건** — 세 reviewer, 착수·종료 여섯 시점 전부.
- **sweep 상태 전이 전수 36조합.** 실제 전이는 셋뿐이고 `completed`·`dead_letter`·
  `recovery_hold`·`pending` 은 어떤 attempts 에서도 불변이다. operator hold 와 살아 있는
  lease 를 sweep 이 우회하지 않는다.
- **큐 진행 24조합 전수. 막힌 조합 0.** 손상 head 가 어떤 durable state 에 있어도 뒤
  command 가 claim 된다 — 단 `F19-1` 재현 B 의 조건(치우기 영구 실패)에서는 막힌다.
- **DB busy 주입.** `BEGIN EXCLUSIVE` 를 쥔 채 `process_next` → `CLAIM_FAILED`, raw 예외
  없음, durable state 무변경. lock 해제 후 정상 회수.
- **sweep commit 과 claim 사이의 실제 interleaving 강제** (round 18 Not Checked 였다).
  중복 치우기 0, 예외 0, 잃은 row 0.
- **`stranded()`·`get()`·`is_unreadable()` signature 무변경, CLI 출력 형식 무변경.**
  `cli.py` 와 `store.py` 의 sha256 이 round 18 target 과 동일하다. wave 12 가 바꾼 src/test
  는 `ingress.py` 와 `test_slack_ack_boundary.py` 둘뿐이고 `T038.yaml` 의 `allowed_paths` 와
  정확히 같다.
- **`contracts/interaction-feedback.md` sha256 이 round 18 과 동일** — 계약문 무변경.
- **`T039` 의 handler 표 14건**을 reviewer 가 독립 AST script 로 재계수해 파일·줄 전부 일치.
- **test 삭제 0, assert 삭제 0.** 삭제된 9줄은 전부 주석과 window 이동이다.
- **test 수 두 방법 모두 1392** (`--collect-only` 요약, `^tests/.*::` 행 계수). 전체 실행
  `1392 passed, 4 deselected`. round 18 의 1388 대비 **+4** 이고 추가 test 수와 같다.
- **본문 미수정 + 각주 판단이 옳다** — contract lens 가 `AGENTS.md` 와 Knowledge Safety 에
  비추어 확인했다.

## Advisory

| id | 위치 | 내용 |
|---|---|---|
| `A19-C1` | `DECISIONS.md:1577`, `:1569`, `STATUS.md:389` | `D-048` 이 "Amends: `D-047` 의 **Scope 절**" 이라 쓰는데 좁힌 문구는 `D-047` 의 **Decision 절**에 있다. `D-047` 의 Scope 절은 지금도 그대로 참이다. **관계 자체는 맞고** 가리키는 절만 틀렸다. 세 자리에서 같은 오기 |
| `A19-C2` | `ingress.py:562`, `cli.py:787` | `interaction-feedback.md:47-54` 인용 범위가 어긋난다. 실제는 48-51 이다. **구분 자체는 살아 있다** |
| `A19-C3` | `T035.md:56`, `T035.yaml:44` | `D-047` 의 넓은 범위를 아직 그대로 적는다. census 건으로 여섯 파일에 각주를 달았는데 같은 성격의 이 둘에는 안 달았다 — **정책 적용이 고르지 않다** |
| `A19-C4` | `T038.md` AC-01/AC-02 | contract 가 evidence 로 `governance stranded` 실측을 요구했는데 `unreadable()` tuple 만 냈다. `cli.py` 가 `forbidden_paths` 라 코드를 안 건드린 것은 맞다 |
| `A19-C5` | `T029.yaml:104` | `invariants:` 의 "아홉" 이 그대로다. 파일 머리 각주가 그 필드를 명시적으로 지목해 redirect 하므로 방어 가능하나, 104줄만 읽으면 지금 사실이 아니다. 같은 각주가 `T032.yaml`·`T033.yaml` 에도 복사됐는데 **그 둘에는 계수를 말하는 `invariants:` 가 없다** |
| `A19-C6` | round 18→19 target diff | `tasks.md`·`doc-impact.json`·`work-contract.json` 이 어느 `allowed_paths` 에도 없다. wave 준비 산출물이라 관행상 문제없다 |
| `A19-C7` | `T040.md` AC-01 표 | 뺀 파일을 둘로 적는데 `approvals.jsonl` 은 round 18 target 에도 없었다. 실제로 뺀 것은 하나다. 산문은 맞고 표만 둘로 읽힌다 |
| `A19-F1` | `ingress.py:332` | **MF4 SURVIVED.** sweep 을 loop 밖 1회로 옮겨도 154건이 통과한다. loop 안 위치를 고정하는 test 가 없다. **(추정)** 등가 mutant 로 보이나, 그렇다면 `A18-3` 이 지적한 반복당 write 비용은 근거 없이 치르는 것이다 |
| `A19-F2` | `cli.py:745-766` | `governance stranded` 에 **truncation 표시가 없다.** `limit` 에 딱 찼을 때 "더 있다" 를 안 알려 `F19-1` 을 operator 가 스스로 알 방법이 없다. `--limit` 의 `min=1` 도 여전히 없다 (`A18-5` 미해결) |
| `A19-F3` | `ingress.py:388-393` | `A18-4` 재확인. sweep 이 `last_error_code` 를 조건 없이 덮는다. 8조합에서 관찰 |
| `A19-F4` | `ingress.py:396-401` | `retry_wait`+소진+`retry_at` 미래인 row 는 sweep 도 `_claim_one` 도 안 잡는다. `stranded()` 는 잡으므로 회수 경로는 있다 |
| `A19-R1` | `test_slack_ack_boundary.py:2902` | `_fill_completed` 가 `INSERT` 를 직접 쓰고 state 를 문자열로 박아 schema 변경에 조용히 어긋날 수 있다. 지금은 정상 |
| `A19-R2` | — | `unreadable()` 이 손상된 `completed` row 를 뺀다는 것을 **직접** 치는 test 가 없다. M11 이 죽는 이유는 `limit` 밀림이지 종결 row 노출이 아니다 |
| `A19-R3` | `T039.md` | `full-pytest` 인용값이 `T038` 과 초 단위까지 같다 — 같은 실행을 복사한 것으로 보인다. 수치 자체는 옳다 |

## Not Checked

- `F19-1` 의 벽 `leased`(살아 있는 lease) 변형 — CHECK 제약으로 재현 못 했다. 다른 다섯
  벽으로 결론은 섰다.
- `A19-F1` MF4 의 등가성 — 반례를 못 만들었을 뿐이다.
- `M13` 을 contract lens 는 재현 안 했다 (regression lens 가 했다).
- **wave 11 중간 상태 대비 test 삭제 여부** — wave 11·12 가 전부 uncommitted 라 비교 기준이
  `HEAD` 하나뿐이다. 1388 → 1392(+4)와 새 test 4개가 맞지만 **삭제 0의 증명은 아니다.**
  검증 path 는 wave 11 시점 commit 을 만드는 것이다.
- 첫 freeze 와 두 번째 freeze 사이에 정말 `T040.md` 한 행만 바뀌었는지 — 첫 freeze 목록이
  보존돼 있지 않다.
- `approvals.jsonl` 의 `proposal_sha256` 재계산 — 해싱 대상 범위가 명시돼 있지 않다.
- 실제 sqlite 장애(disk full, read-only, WAL 손상). 세 라운드 연속 같은 항목이 남는다.
- `_sweep_recoverable` 의 두 UPDATE 사이 process 사망.
- `unreadable()` 의 실제 지연 비용, `A18-3` 의 수치.
- `OutboxDispatcher`(`A18-6`)와 `apply_jobs.py` 의 같은 형태 sweep — target 밖이다.
- verifier v2 전체 재실행 — pytest 만 재현했다 (별도로 PASS 확인됨).
