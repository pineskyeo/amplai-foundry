# Round 21 — Contract Lens (MGC-012-P5 Wave 14)

**판정: FAIL.** 새 blocker 4건 — P0 0 / P1 0 / Blocking-P2 4. Advisory 6.

round 20 의 blocker 6건 중 **5건 닫힘, 1건 부분 닫힘**(`C20-1`).

## Target Integrity

`specs/003-slack-proposal-card/evidence/review-target-round-21.txt` 의 **행별 hash 를 다시
계산**했다. aggregate 는 무손상 근거로 쓰지 않는다 — aggregate 는 행 문자열만 해싱하므로
행이 가리키는 파일이 뒤에 바뀐 것을 못 잡는다.

| 시점 | 결과 |
|---|---|
| 착수 | `rows 97 \| 어긋남 0 []` |
| 중간 (mutation 여섯 회 뒤) | `rows 97 \| 어긋남 0 []` |
| 종료 | `rows 97 \| 어긋남 0 []` (아래 Closing Re-check) |

`git status --short` 는 착수·중간에 빈 출력이고 종료 시점에는 이 보고서 파일 하나만
untracked 로 나온다. mutation 은 전부 `PYTHONDONTWRITEBYTECODE=1`
로 돌렸고 착수 전에 `find . -name __pycache__ -exec rm -rf {} +` 를 했다.

round 20 target 92 파일 중 92개를 그대로 이어받고 5개가 늘어 97개다. **변경된 파일 11개**를
독립 계산으로 뽑았다 — `cli.py`, `test_cli.py`, `test_slack_ack_boundary.py`,
`CURRENT_ITEM.md`, `DECISIONS.md`, `STATUS.md`, `doc-impact.json`, `T038.md`, `index.yaml`,
`tasks.md`, `work-contract.json`. **`src/amplai_foundry/governance/ingress.py` 는 안 바뀌었다** —
`T044` 의 `forbidden_paths` 가 `src/amplai_foundry/governance/` 를 막았고 그대로 지켜졌다.

## Round 20 Blockers — 5 Closed, 1 Partial

| round 20 | 등급 | 닫힘 | 확인 방법 |
|---|---|---|---|
| `F20-1` | P1 | **닫힘 (보이게)** | round 20 이 실측한 시나리오를 재현했다 — 손상 `dead_letter` **101개** / `--limit 100` → `UNREADABLE` 100줄 + `...  목록이 --limit 100 에서 잘렸다. 더 있다`. mutation M18(truncation 줄을 `if False:`) → `..._says_when_the_list_is_cut`·`..._reports_a_cut_in_either_list` **2건 사망** |
| `F20-2` | B-P2 | **닫힘 (보이게)** | 후보 **301개** / `--limit 100` → 100줄 + 잘림 줄. **두 목록이 각각 판정되는지 mutation 으로 분리 확인**: `or len(unreadable) > limit` 만 지우면 `..._reports_a_cut_in_either_list` 사망, `len(commands) > limit` 만 지우면 `..._says_when_the_list_is_cut` 사망. **두 항이 각각 test 로 고정돼 있다** |
| `F20-3` | B-P2 | **닫힘** | M19(`unreadable()` 의 `WHERE state != 'completed'` 제거) → `..._skips_a_corrupt_row_that_already_completed` **1건 사망** |
| `C20-1` | B-P2 | **부분** | `T038.md:87` 에 정정 각주 실재(`:89-98`)하고 표의 `limit 의 의미` 행(`:17`)도 각주 본문이 함께 짚는다. **그러나 같은 주장을 하는 형제 위치 둘이 정정 없이 남았다** → `C21-4` |
| `C20-2` | B-P2 | **닫힘** | `DECISIONS.md:1676` 의 `Source` 가 "**벽 다섯**" 으로 고쳐졌고 `Correction` 항목이 경위를 적는다. `Reason` 절(넷)과 더는 안 어긋난다 |
| `R20-1` | B-P2 | **닫힘** | M20(sweep 둘째 UPDATE 에 `COALESCE(last_error_code, …)`) → `..._leaves_the_prior_error_code_alone[None]` **1건 사망**. `[None]` param 이 없으면 안 죽는다는 evidence 의 서술도 재현된다 |

`F20-1`·`F20-2` 는 **없어진 것이 아니라 보이게 됐다.** `D-050` 과 `T044` evidence 가 그
구분을 명시하고, 실측이 그 서술과 맞는다 — 잘린 row 는 여전히 `--limit` 을 올려야 회수된다.

## Wave 14 가 한 주장 다섯 — 대조

| 주장 | 판정 | 근거 |
|---|---|---|
| 1. `D-050` 이 `F20-1` 을 truncation 표시로 닫았다 | **참** | 위 `F20-1` 행 |
| 2. 두 목록 각각을 판정한다 | **참** | 두 항을 각각 지우는 mutation 이 각각 다른 test 를 죽인다 |
| 3. `F20-3`·`R20-1` 의 계약 둘을 test 로 묶었다 | **참** | M19·M20 둘 다 KILLED |
| 4. `C20-1`·`C20-2` 를 고쳤다 | **부분** | `C20-2` 는 참. `C20-1` 은 지목된 자리만 고쳤다 → `C21-4` |
| 5. 새 구조가 요구하는 것을 착수 전에 표로 만들고 첫 항목을 막았다 | **부분** | `--limit 0` 우회는 실제로 막혔고 M21 이 그것을 죽인다. **그러나 표가 전수가 아니다** → `C21-2`. 표의 한 칸은 test 가 대상을 안 친다 → `C21-1`. "착수 전" 이라는 시간 순서 자체는 manifest 의 자기 진술 외에 확인할 길이 없다 |

## New Blockers

| id | 등급 | 위치 | 결함 |
|---|---|---|---|
| `C21-1` | **Blocking-P2** | `evidence/MGC-012-P5-T044.md:39` (AC-07 표 #3), `cli.py:765` | **표의 칸이 "test 있음" 인데 그 test 가 경계를 안 친다.** #3 은 "정확히 `limit` 개일 때 안 잘림 / 처리 `len > limit` / test `..._stays_quiet_when_the_list_fits`" 다. **그 test 는 기본 `limit=100` 에 row 1개다 — 경계에 서지 않는다.** `truncated = len(...) > limit` 을 `>= limit` 으로 바꾸면 **1409개가 전부 통과한다** (SURVIVED, 전 suite 로 확인). `>=` 면 정확히 `limit` 개일 때 없는 잘림을 보고한다. AC-07 이 스스로 "**test 칸은 mutation 으로 확인한 것만 채운다**" 를 규정하는데 이 칸은 mutation 없이 채워졌다. round 19 `R19-1`·`R19-2` 와 같은 형태다 |
| `C21-2` | **Blocking-P2** | `cli.py:753-754`, AC-04(`T044.yaml:103-106`), AC-07 표 | **`limit + 1` 이 위쪽 끝에 raw traceback 을 새로 열었다.** `--limit 9223372036854775807`(int64 최대)이 `limit + 1 = 2^63` 이 되어 `sqlite3` 가 `OverflowError` 를 낸다. CLI 의 `except` 는 `GovernanceStoreError`·`GovernanceMigrationError`·`IngressError`·`ValueError`·`sqlite3.Error` 만 잡고 **`OverflowError` 는 그 어느 것도 아니다.** 실제 CLI 로 실측 — 전체 traceback 이 그대로 나간다. `--limit 9223372036854775806` 은 정상이다. **wave 14 이전에는 둘 다 정상이었다** (`limit=limit` 로 되돌리는 mutation 으로 확인). round 16 `FR-3`·`R16-1` 이 닫은 "CLI 가 raw traceback 을 내지 않는다" 가 되돌아왔다. AC-04 는 `limit + 1` 의 **아래쪽 끝**(`0`·`-1`)만 세고 위쪽 끝을 안 셌다 |
| `C21-3` | **Blocking-P2** | `.ai-team/policy/approvals.jsonl`, `evidence-trace.jsonl:19`, `DECISIONS.md:1686`, `work-contract.json` `human_gates` | **승인 절차가 어긋난다.** `D-050` 은 `Status: APPROVED` 이고 Consequence 가 "**CLI 출력 형식이 바뀐다**" 를 명시하며 `MGC-012-P5-W14` contract 의 `human_gates` 에 `public_contract` 가 들어 있다. **그런데 `approvals.jsonl` 에 `D-050` 행이 없다** — `D-047`·`D-048`·`D-049` 는 셋 다 `public_contract` 행이 있다. `evidence-trace.jsonl` 은 `D-050` 을 `{"gate": "none"}` 으로 기록한다. **네 Decision 중 실제로 사용자-facing 출력을 바꾸는 유일한 것이 승인 ledger 를 안 탔다.** 두 파일 모두 freeze target 밖이라 hash 로는 안 잡힌다 |
| `C21-4` | **Blocking-P2** | `task-manifests/MGC-012-P5-T038.yaml:38`, `CURRENT_ITEM.md:166` | **`C20-1` 을 고친 wave 가 같은 지적을 재생산했다.** `D-048` 의 `Amended` 와 `D-049` 의 `Amends` 가 틀렸다고 지목한 주장("`limit` 이 후보 상한으로 돌아온다")이 **두 자리에 정정 없이 그대로 서 있다** — `T038.yaml:38` 의 `scope.include` 와 `CURRENT_ITEM.md:166` 의 `T038` 절이다. wave 14 는 `T038.md:87` 한 자리만 각주를 달았다. `T039` 가 세운 규칙("완료된 wave 의 manifest 와 evidence 본문은 고치지 않는다. 대신 **정정 각주**를 단다")이 manifest 를 명시적으로 포함하는데 manifest 쪽이 빠졌다. **round 20 이 `C20-1` 에 붙인 이름 그대로 "선택적 누락" 이다.** round 17 `F17-4`~`F17-6` 과 같은 형태다 |

### `C21-1` 의 실측

```text
mutation: truncated = len(commands) >= limit or len(unreadable) >= limit
전 suite  → rc=0, 죽은 test 0건 (SURVIVED)

경계 동작 실측 (pristine 코드):
  후보 10개 / --limit 10 → 10줄, 잘림 줄 없음   ← 코드는 옳다
  후보 6개  / --limit 3  → 3줄 + 잘림 줄
```

**코드는 옳고 test 만 없다.** 그래서 다음 라운드가 이 칸을 "이미 막았다" 로 분류한다.

### `C21-2` 의 실측

```text
$ .venv/bin/python -m amplai_foundry.cli governance stranded \
      --workspace <ws> --limit 9223372036854775807
  ... (전체 traceback)
  OverflowError: Python int too large to convert to SQLite INTEGER

$ ... --limit 9223372036854775806        → 정상 출력, exit 0

같은 명령을 `limit=limit`(wave 14 이전)로 되돌리면 둘 다 exit 0.
```

## 형제 위치 계수 — 두 방법

`D-049` 가 틀렸다고 지목한 주장이 남아 있는 자리를 두 방법으로 셌다.

- 방법 1 — `grep -rn "후보 상한"` (`.git` 제외, review 문서 제외): **10 자리**
- 방법 2 — `grep -rn "stranded() 와 같은\|같은 규칙이다\|같은 의미를 갖는다\|stranded() 와 같게"`: **6 자리**

두 방법의 합집합을 분류하면 아래와 같다.

| 자리 | 상태 |
|---|---|
| `T038.md:87` | 정정 각주 있음 (wave 14) |
| `T038.md:17` (표 행) | 위 각주 본문이 명시적으로 짚음 |
| `DECISIONS.md:1595` (`D-048` 본문) | `Amended`(`:1622`) 있음 |
| `DECISIONS.md:1622`·`1631`·`1671` | 정정문 자신 |
| `STATUS.md:442` | 정정 서술 |
| `T041.yaml:66` | 처음부터 옳다 ("저쪽은 후보 상한, 이쪽은 출력 상한") |
| `ingress.py:640`·`646` | 처음부터 옳다 |
| **`T038.yaml:38`** | **정정 없음** → `C21-4` |
| **`CURRENT_ITEM.md:166`** | **정정 없음** → `C21-4` |
| `test_slack_ack_boundary.py:2962` | round 20 `A20-C3` Advisory, 미해결 → `A21-C1` |

## Verified Clean

- **target 무손상.** 착수·중간·종료 세 시점 전부 `rows 97 | 어긋남 0`. aggregate 를 근거로
  쓰지 않았다.
- **`ingress.py` 무변경.** `T044` 의 `forbidden_paths` 대로다. round 20 이 `F20-1`·`F20-2` 를
  "코드로 없앨 수 없다" 로 판정했고 `D-050` 이 그 판단을 따랐다.
- **`allowed_paths` 준수.** round 20→21 diff 11 파일이 전부 `T044`(`cli.py`·test 둘) 또는
  `T045`(`specs/003-slack-proposal-card/`·`docs/workstreams/messenger-governance-closure-v3/`)
  의 `allowed_paths` 안이다.
- **mutation 여섯 전부 결과가 evidence 와 일치.** M18(2건)·M19(1건)·M20(1건)·M21(2건) 이
  KILLED 이고 죽은 test 이름까지 `T044.md` 의 표와 같다. 추가로 두 mutation(두 항 각각 제거)
  을 넣어 `F20-2` 의 "각각 판정" 을 분리 확인했다.
- **test 삭제 0, skip/xfail 신규 0 — 이번에 처음 증명됐다.** package 5 가 commit 됐으므로
  기준 commit 이 생겼다. `git diff 0501b75..HEAD -- tests/` 는 `710 insertions, 14 deletions`
  이고 삭제된 `def test_` 가 **0개**, 새로 붙은 `skip`·`xfail` 이 **0개**다. 세 라운드 연속
  Not Checked 이던 항목이다.
- **test 수 두 방법 모두 1409.** 파일별 계수 합산 1409, 전체 실행 `1409 passed, 4 deselected`.
  round 20 의 1401 대비 **+8** 이고 추가 test(단독 4 + parametrize 2×2)와 같다.
  targeted 도 27 + 118 = **145** 로 evidence 와 같다.
- **lint·type 전부 재현.** `ruff check` All checks passed, `ruff format --check` 138 files,
  `mypy` 102 source files Success, `git diff --check` 빈 출력. evidence 의 숫자와 같다.
- **`D-050` 이 `T026` 의 `scope.exclude` 를 정확히 인용한다.** `T026.yaml:52` 가 배제한 것은
  "새 출력 형식(JSON 등)의 계약화" 이고 잘림 한 줄은 그 배제에 안 걸린다.
- **`D-050` Scope 준수.** `IngressService` 의 signature 가 하나도 안 바뀌었다.
  `stranded()`·`unreadable()` 의 src 호출 지점은 `cli.py` 둘뿐이고 그 둘만 바뀌었다.
- **`D-050` 의 두 Rejected 가 상위 결정과 안 부딪친다.** `dead_letter` 정리 경로는 `D-038`
  항목 1 이 거절한 것이고, service flag 는 `D-047`·`D-048`·`D-049` 의 공통 불변을 지킨다.
- **`C20-2` 의 정정이 승인 판단을 안 바꾼다.** `D-049` 의 `Reason` 은 처음부터 넷을 댔고
  결론이 다섯이든 여섯이든 같다.
- **`T045` 가 약속한 Stop rule 이 실재한다.** `CURRENT_ITEM.md:412` — "manifest 를 복사해
  만들 때 `status` 와 `completion` 을 먼저 되돌린다".
- **`index.yaml` 과 manifest 의 `status` 가 지금은 일치한다.** `T044`·`T045` 둘 다 `done`.
- **`tasks.md` 에 `T044`·`T045` 가 `(done)` 으로 실재한다.**
- **CLI 출력을 읽는 문서·script 가 없다.** `docs/`·`scripts/`·`.ai-team/` 어디에도
  `governance stranded` 출력을 파싱하는 곳이 없어 한 줄 추가가 깨는 소비자가 없다.

## Advisory

| id | 위치 | 내용 |
|---|---|---|
| `A21-C1` | `test_slack_ack_boundary.py:2962` | **`A20-C3` 두 라운드째 미해결.** assert message 가 "`limit` 은 **후보 상한**이어야 한다" 다. `D-049` 뒤에는 틀린 문구다. test 는 옳게 통과한다. 같은 파일 `:3116` 은 옳게 적혀 있어 대조가 된다 |
| `A21-C2` | `T044.md:33` (AC-07 표 #2) | `--limit -1` 칸이 "같은 guard 가 먼저 잡는다 / test `[-1]`" 인데 **M21(CLI guard 제거)이 `[0]` 만 죽이고 `[-1]` 은 안 죽인다.** `-1` 은 `limit + 1 = 0` 이 되어 service guard 가 잡으므로 CLI guard 없이도 통과한다. **요구(거부)는 지켜지므로 blocker 가 아니다.** 다만 그 칸의 "처리" 서술을 고정하는 test 는 아니다 |
| `A21-C3` | `DECISIONS.md:1605` | **`A20-C2` 두 라운드째 미해결.** `D-048` 의 첫 `Rejected`("`LIMIT` 을 없애고 전체를 훑기")가 `D-049` 가 실제로 채택한 방식인데 pointer 가 없다. `D-048` 만 읽으면 여전히 거절된 것으로 보인다 |
| `A21-C4` | `cli.py:784` | 잘림 줄이 **어느 목록이 잘렸는지 안 말한다.** 판정은 각각 하지만 출력은 하나로 합친다. `D-050` 이 "한 줄로 알린다" 로 정했으므로 결정 위반은 아니다. operator 는 `--limit` 을 올려야 어느 쪽인지 안다 |
| `A21-C5` | `work-contract.json` `status` | wave 14 가 끝났는데 contract 의 `status` 가 `"ready"` 다. `blocked_by` 도 빈 문자열이다. 이전 wave 의 값과 대조할 수 없다 — 이 파일은 wave 마다 덮어쓰이고 commit 이 하나뿐이다 |
| `A21-C6` | `T045.yaml:39` | 문서 전용 task 인데 `allowed_paths` 에 `src/amplai_foundry/governance/ingress.py` 가 들어 있다. `T042` 복사의 잔재다. **실제로 쓰이지 않았다** — `ingress.py` 는 안 바뀌었다 |

## Not Checked

- **`C21-2` 가 `search --limit` 등 다른 CLI 경로에도 있는지.** `search` 는 vault 도메인이라
  `MGC-012` 범위 밖이고 `min=1` 이 붙어 있으며 `limit + 1` 구조가 없다. **위쪽 끝은 안 쟀다.**
- **`C21-3` 의 승인이 다른 곳에 기록됐는지.** repository 전역 `grep "D-050"` 으로
  `approvals.jsonl` 밖의 승인 record 를 못 찾았다. **대화 기록은 확인할 수 없다.**
- **`limit + 1` 의 추가 scan 비용.** `T044` 가 `non_goals` 로 뒀고 `A20-F1` 의 측정을
  인용한다. 이번 라운드에서 다시 재지 않았다.
- **"새 구조 요구 표를 착수 전에 만들었다" 는 시간 순서.** manifest 의 `risk.concerns` 가
  첫 항목을 담고 있는 것까지만 확인했다. manifest 자체가 같은 wave 의 산출물이라
  자기 진술이다.
- **v2 profile 전체.** `pytest`·`ruff`·`mypy`·`diff-check` 는 직접 돌려 재현했지만
  `loop-runtime-doctor`·`schema`·`vault-lint`·`project-pack` 은 안 돌렸다.
- **실제 sqlite 장애(disk full, read-only, WAL 손상).** 다섯 라운드 연속 같은 항목이다.
- `doc-impact.json` 450줄의 내용 정합성. 생성물이고 이번 wave 의 주장과 무관하다.

## Closing Re-check

```text
$ .venv/bin/python - <<'EOF'   # target 머리말의 script 그대로
rows 97 | 어긋남 0 []
$ git status --short
?? specs/003-slack-proposal-card/evidence/round-21-lens-contract.md
```

**변경은 이 보고서 파일 하나뿐이고 target 97행 밖이다.**

mutation 은 전부 되돌아갔고 target 안의 어떤 파일도 쓰지 않았다. 이 보고서는 target 밖의
새 파일이다.
