# Round 21 — Regression Lens (MGC-012-P5 Wave 14)

**판정: FAIL.** blocker 2건 — P0 0 / P1 0 / Blocking-P2 2. Advisory 5.

Target: `evidence/review-target-round-21.txt`, 97 파일. 착수와 종료 두 시점에 **행별 hash
재계산**으로 확인했고 둘 다 **어긋남 0** 이다. **aggregate 를 무손상 근거로 쓰지 않았다** —
aggregate 는 manifest 의 행 문자열만 해싱하므로 행이 가리키는 파일의 뒤바뀜을 못 잡는다.

mutation 14건을 전부 `PYTHONDONTWRITEBYTECODE=1` 과 `__pycache__` 삭제 뒤에 돌렸다.
매번 되돌리고 `git diff --quiet` 로 원상을 확인했다.

## Verdict In One Line

wave 14 는 round 20 의 회귀 계열 blocker 둘(`F20-3`·`R20-1`)을 **전부 닫았다.** 그런데
**같은 wave 가 만든 AC-07 표에 거짓 칸이 둘 있다.** round 19 `R19-1`·`R19-2`, round 20 이
전수 검증으로 0 을 확인했던 그 형태가 이번 표에서 되살아났다. `T044` manifest 가 자기
규칙으로 넣은 **"표의 칸을 mutation 으로 확인하기 전에는 채우지 않는다"** 를 두 칸에서
지키지 않았다.

## Round 20 Regression Blockers — 닫힘 표

| round 20 | 등급 | mutation | 판정 | 죽은 test | 결론 |
|---|---|---|---|---|---|
| `R20-1` | B-P2 | M20 — sweep 둘째 UPDATE 에 `last_error_code = COALESCE(last_error_code, 'INGRESS_ATTEMPTS_EXHAUSTED')` | **KILLED** | `test_the_exhaustion_sweep_leaves_the_prior_error_code_alone[None]` — 1건 | **닫힘** |
| `F20-3` | B-P2 | M19 — `unreadable()` 의 `"WHERE state != 'completed' "` 행 삭제 | **KILLED** | `test_unreadable_skips_a_corrupt_row_that_already_completed` — 1건 | **닫힘** |

`R20-1` 은 round 20 에서 SURVIVED 였다. 이제 죽는다. **`[None]` 만 죽고
`[INGRESS_AUTHORITY_DENIED]` 는 안 죽는다** — `COALESCE` 는 `NULL` 일 때만 덮기 때문이다.
`T044` evidence 가 그 사실을 스스로 적었고 실측이 그것과 일치한다.

값이 있는 쪽 parametrize 도 놀고 있지 않다. 같은 자리를 `COALESCE` 없이 무조건 덮는
mutation(M-R21g)을 넣으면 **3건이 죽는다** — `[INGRESS_AUTHORITY_DENIED]`, `[None]`,
그리고 형제 test `test_an_exhausted_corrupt_row_is_still_visible_after_the_sweep_clears_it`.
두 parametrize 가 서로 다른 mutant 를 잡는다.

## Wave 14 표 전수 검증 — AC-07 (`limit + 1` 이 요구하는 것)

`T044` evidence 의 여덟 칸을 하나씩 쳤다. **거짓 칸 2, 약한 칸 1, 참 4, 정직한 빈 칸 2**
(#7·#8 은 test 칸을 비워 뒀고 그 사유가 맞다).

| # | 표가 적은 test 칸 | mutation | 판정 | 죽은 test | 결과 |
|---|---|---|---|---|---|
| 1 | `..._still_rejects_a_non_positive_limit[0]` (M21) | M21 — CLI 의 `if limit < 1: _fatal(...)` 두 줄 삭제 | **KILLED** | `test_stranded_still_rejects_a_non_positive_limit[0]`, `test_an_invalid_limit_fails_closed` — 2건 | **참** |
| 2 | 같은 test `[-1]` | M21 (같은 것) | `[-1]` 은 **안 죽는다** | — | **약함** → `A21-R1` |
| 3 | `..._stays_quiet_when_the_list_fits` | M-R21a — `len(...) > limit` 둘을 `>= limit` 로 | **SURVIVED** (1409 전부 통과) | 없음 | **거짓** → `R21-1` |
| 4 | `..._says_when_the_list_is_cut` (M18) | M18 — `if truncated:` → `if False:` | **KILLED** | `test_stranded_says_when_the_list_is_cut`, `test_stranded_reports_a_cut_in_either_list` — 2건 | **참** |
| 5 | `..._reports_a_cut_in_either_list` | M-R21c — `or len(unreadable) > limit` 삭제 | **KILLED** | `test_stranded_reports_a_cut_in_either_list` — 1건 | **참** |
| 5' | (표에 없음, 반대쪽) | M-R21d — `len(commands) > limit or` 삭제 | **KILLED** | `test_stranded_says_when_the_list_is_cut` — 1건 | **참** |
| 6a | `..._says_when_the_list_is_cut` 이 줄 수를 센다 | M-R21b — `commands[:limit]` → `commands[: limit + 1]` | **KILLED** | `test_stranded_says_when_the_list_is_cut` (`assert 4 == 3`) — 1건 | **참** |
| 6b | 같은 칸 | M-R21f — `unreadable = unreadable[:limit]` **행 삭제** | **SURVIVED** (1409 전부 통과) | 없음 | **거짓** → `R21-2` |
| 7 | "논리로 확인. test 없음" | — | 검증 안 함 | — | **정직한 빈 칸** |
| 8 | "없음 — `non_goals`" | — | 검증 안 함 | — | **정직한 빈 칸** |

추가로 새 구조 자체를 쳤다.

| mutation | 판정 | 죽은 test |
|---|---|---|
| M-R21e — `ingress.stranded(limit=limit + 1)`·`unreadable(limit=limit + 1)` 의 `+ 1` 제거 | **KILLED** | `..._says_when_the_list_is_cut`, `..._reports_a_cut_in_either_list` — 2건 |
| M-R21h — `> limit` 둘을 `> limit + 1` 로 | **KILLED** | 같은 2건 |

M-R21h 가 죽고 M-R21a 가 사는 것이 `R21-1` 의 정확한 모양이다. **거짓 음성 방향(잘렸는데
침묵)은 막혀 있고 거짓 양성 방향(안 잘렸는데 잘렸다고 말함)만 뚫려 있다.**

## New Blockers

| id | 등급 | 위치 | 결함 |
|---|---|---|---|
| `R21-1` | **Blocking-P2** | `src/amplai_foundry/cli.py:765`, 표 칸은 `evidence/MGC-012-P5-T044.md:34` (#3) | **`>` 를 `>=` 로 바꿔도 1409 개가 전부 통과한다** (M-R21a SURVIVED, 전 suite 확인). 표 #3 의 요구가 "정확히 `limit` 개일 때 안 잘림" 이고 test 칸에 `..._stays_quiet_when_the_list_fits` 를 채웠는데 **그 test 는 후보 1개에 기본 `limit=100` 이라 경계를 한 번도 안 친다.** `len == limit` 인 입력을 주는 test 가 suite 에 하나도 없다 |
| `R21-2` | **Blocking-P2** | `src/amplai_foundry/cli.py:767`, 표 칸은 `evidence/MGC-012-P5-T044.md:37` (#6) | **`unreadable = unreadable[:limit]` 을 통째로 지워도 1409 개가 전부 통과한다** (M-R21f SURVIVED, 전 suite 확인). 표 #6 의 처리는 "출력을 `limit` 개로 자른다 / slicing" 이고 slicing 은 둘인데 **test 칸이 댄 `..._says_when_the_list_is_cut` 은 손상 row 가 0개인 상황이라 `commands` 쪽만 센다.** `unreadable` 쪽 상한은 무방비다 |

### 둘 다 등가 mutant 가 아니다 — 실측

`R21-1`. stranded 후보를 정확히 6개로 만들고 `--limit` 을 5·6·7 로 돌렸다.

```text
pristine    limit=5  줄=5  잘렸다=True
            limit=6  줄=6  잘렸다=False      ← 정확히 limit
            limit=7  줄=6  잘렸다=False
mutant(>=)  limit=5  줄=5  잘렸다=True
            limit=6  줄=6  잘렸다=True       ← 안 잘렸는데 "더 있다"
            limit=7  줄=6  잘렸다=False
```

`limit == 후보 수` 는 드문 경우가 아니다. `--limit` 을 올려 회수하는 것이 `D-050` 이 정한
운영 절차이고, **operator 는 올린 뒤에 "더 있다" 를 보면 또 올린다.** 잘못된 "더 있다" 는
`D-050` 이 없애려던 바로 그 오해를 반대 방향으로 만든다.

`R21-2`. 손상 row 6개에 `--limit 2` 로 돌렸다.

```text
pristine  UNREADABLE 줄 수 = 2   잘렸다=True
mutant    UNREADABLE 줄 수 = 3   잘렸다=True      ← --limit 2 인데 3줄
```

`limit + 1` 을 요청한 그 한 줄이 그대로 출력에 샌다. `D-050` 의 Decision 문이 "출력은
`limit` 개까지만 한다" 로 못박은 것이고 `stranded` 쪽만 지켜진다.

### 왜 이것이 빈 칸보다 나쁜가

착수 지시가 적은 그대로다. **#3 과 #6 은 채워져 있어 "이미 막았다" 로 분류된다.** 표를
읽는 다음 wave 는 그 자리를 안 본다. `T044` manifest 가 `implementation.guidance` 에 넣은
자기 규칙이 정확히 이것을 막으려던 것인데(`round 19 R19-3`), M20 한 칸에서만 작동했고
#3·#6 에서는 mutation 을 아예 안 돌렸다. 표는 여덟 칸인데 mutation 은 넷만 돌았다 —
**evidence 의 mutation 표(M18~M21)가 표의 칸 수와 안 맞는 것이 그 신호였다.**

## Verified Clean

- **행별 hash 재계산 — 착수·종료 두 시점 모두 `rows 97 | 어긋남 0`.** aggregate 를 근거로
  쓰지 않았다.
- **`F20-3`·`R20-1` 둘 다 닫힘.** M19·M20 이 각각 다른 test 하나씩을 죽인다.
- **`R20-1` 의 parametrize 두 값이 서로 다른 mutant 를 잡는다.** `[None]` 은 M20 을,
  `[INGRESS_AUTHORITY_DENIED]` 는 M-R21g(무조건 덮기)를 잡는다. 어느 쪽도 죽은 무게가 아니다.
- **test 삭제 0.** 기준 commit `e13e01c`(package 5 의 네 wave 가 들어오기 직전) 대비
  `git diff e13e01c 87a0c76 -- tests/` 에서 `-` 로 시작하는 `def test_` 행이 **0** 이고,
  `assert`·`def `·`@pytest` 를 지운 행도 **0** 이다. 세 라운드 연속 "기준 commit 이 없다" 로
  Not Checked 이던 항목이 닫혔다.
- **skip·xfail 신규 0.** repository 전체 `tests/` 에 `pytest.skip` 이 둘뿐이고
  (`test_slack_http.py:2358`·`:2704`) 둘 다 `git diff e13e01c 87a0c76` 에 안 나온다 —
  그 파일은 손대지 않았다. `xfail` 은 0건이다. `git diff` 의 `+skip` 검색이 낸 한 건은
  test **이름**의 `_skips_` 였다.
- **test 수 두 방법 일치.** `--collect-only` = **1409 selected**(1413 collected, 4
  deselected), 실행 = **1409 passed, 4 deselected**. 파일별 계수의 합도 **1409** 다.
  round 20 의 1401 대비 **+8** 이고 새 test 함수 6개(그중 2개가 parametrize 2)의 합과 같다.
- **wave 14 가 이름을 댄 test 6개가 전부 실재한다.** `test_cli.py:494`·`:515`·`:527`·`:551`,
  `test_slack_ack_boundary.py:3203`·`:3226`.
- **fixture 가 검사 대상을 안 지운다.** `_readable_wall`(`test_slack_ack_boundary.py:3053`)은
  `INSERT` 만 한다. `DELETE`·`DROP` 이 없다.
  `test_stranded_reports_a_cut_in_either_list` 의 `UPDATE ... WHERE state='pending'` 은
  벽만 친다 — 검사 대상인 stranded row 는 `pending` 이 아니다.
- **`connect()` 호출 수가 안 늘고 안 줄었다.** `git diff e13e01c 87a0c76 --
  src/amplai_foundry/cli.py` 에서 `governance stranded` 의 store 접근은 그대로
  `ingress.stranded()` 하나 + `ingress.unreadable()` 하나다. 인자만 `limit` → `limit + 1`
  로 바뀌었다. filesystem-guard test 둘(`test_governance_store.py:1077`
  `test_connect_normalizes_a_filesystem_guard_failure[before-validate|after-validate]`)은
  `_FlippingFilesystemProbe` 로 **하나의 `connect()` 안**의 `inspect()` 호출을 세고
  `GovernanceStore` 를 직접 만든다. CLI 를 안 거치므로 창이 안 밀린다. 두 test 는 wave 14
  뒤에도 통과한다.
- **`test_governance_store.py` 의 wave 14 diff 는 주석 문구 셋뿐이다** ("아홉" → "열").
  단언은 한 줄도 안 바뀌었다.
- **전 suite 통과.** `1409 passed, 4 deselected in 158.04s`, exit 0.
- **mutation 14건 전부 되돌렸다.** 종료 시점 `git diff --stat` 이 비었고 추적 안 되는 파일은
  세 lens 의 보고서뿐이다.

## Advisory

| id | 위치 | 내용 |
|---|---|---|
| `A21-R1` | `evidence/MGC-012-P5-T044.md:33` (표 #2) | 표 #2 의 처리가 "같은 guard 가 먼저 잡는다"(= CLI guard)인데 **M21 로 CLI guard 를 통째로 지워도 `[-1]` 은 안 죽는다.** `--limit -1` 은 `limit + 1 = 0` 이 service 의 `limit < 1` 에 걸려 여전히 exit 1 이다. 요구("-1 이 거부된다")는 test 로 고정되지만 **처리 칸이 말하는 경로는 그 test 가 안 친다.** `[0]` 만 CLI guard 를 친다 |
| `A21-R2` | `tests/test_slack_ack_boundary.py:3114`, `:3146` | **`A20-R1` 두 라운드째 미해결.** `poison in ingress.unreadable(limit=100)` 이 형제들(`:2499`·`:2518`·`:2780`·`:2841`·`:2961`)의 `==` 보다 약하다. wave 14 의 `allowed_paths` 안이었지만 안 고쳤다 |
| `A21-R3` | `src/amplai_foundry/cli.py:784` | truncation 문구 전체를 `"잘렸다"` 한 단어로 줄여도 27건이 통과한다. test 가 `"잘렸다" in stdout` 만 보므로 **`--limit N` 값도 "더 있다" 도 안 고정된다.** `D-050` 이 문구를 계약화하지 않았으므로 결함은 아니다 |
| `A21-R4` | `src/amplai_foundry/cli.py:747` | guard 문구를 `"XX"` 로 바꿔도 27건이 통과한다. `test_an_invalid_limit_fails_closed` 와 `..._still_rejects_a_non_positive_limit` 이 exit code 와 traceback 부재만 본다. `T044` 의 handoff 가 "**같은 문구로** 여기서 잡는다" 를 적었는데 그 "같은 문구" 를 지키는 test 가 없다 |
| `A21-R5` | `src/amplai_foundry/cli.py:199` vs `:734` | 같은 파일의 다른 command 는 `typer.Option("--limit", min=1)` 로 선언 단계에서 막고 `stranded` 는 본문 `if limit < 1` 로 막는다. 두 방식이 공존한다. `stranded` 는 `limit + 1` 때문에 본문 guard 가 필요하지만 **`min=1` 을 함께 걸지 못할 이유는 없다** |

## Not Checked

- **wave 14 단독의 test 삭제 0.** `87a0c76` 한 commit 에 wave 11~14 가 함께 들어 있어
  wave 14 만 분리한 기준 commit 이 없다. 위 증명은 **네 wave 를 묶어** `e13e01c` 대비로 한
  것이다. round 20 시점(wave 13 직후)의 test 파일 내용은 commit 되지 않았으므로 복원할 수
  없다.
- `unreadable()` 의 `limit` 채움 `break` 와 `_stranded_rows` 의 SQL `LIMIT` — round 20
  regression lens 가 이미 mutate 해 KILLED 를 확인했고 wave 14 가 그 줄들을 안 건드렸다.
  이번 라운드에서 다시 안 돌렸다.
- `stranded()`·`unreadable()` 을 CLI 밖에서 부르는 곳(`OutboxDispatcher`·`apply_jobs.py`)의
  같은 truncation 형태 — target 밖이다. round 20 도 같은 이유로 안 봤다.
- 여러 process 동시성, 실제 sqlite 장애(disk full·read-only·WAL 손상) — **다섯 라운드 연속**
  같은 항목이다.
- `R21-1`·`R21-2` 의 mutation 을 **동시에** 넣었을 때의 상호작용. 각각 단독으로만 쳤다.

## Reproduction

```bash
# 무손상 확인 (착수·종료 두 번)
.venv/bin/python - <<'EOF'
import hashlib,pathlib
p=pathlib.Path('specs/003-slack-proposal-card/evidence/review-target-round-21.txt')
rows=[l for l in p.read_text().splitlines() if l and not l.startswith('#')]
bad=[]
for r in rows:
    d,n=r.split('  ',1)
    if d!=hashlib.sha256(pathlib.Path(n).read_bytes()).hexdigest(): bad.append(n)
print('rows',len(rows),'| 어긋남',len(bad),bad)
EOF
# → rows 97 | 어긋남 0 []

# 수 세기 두 방법
find . -name __pycache__ -prune -exec rm -rf {} +
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest --collect-only   # 1409/1413 (4 deselected)
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -o addopts="" -m "not slack_e2e" -q
# → 1409 passed, 4 deselected

# R21-1
sed -i '' 's/len(commands) > limit or len(unreadable) > limit/len(commands) >= limit or len(unreadable) >= limit/' src/amplai_foundry/cli.py
find . -name __pycache__ -prune -exec rm -rf {} +
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -o addopts="" -m "not slack_e2e" -q   # 1409 passed
git checkout -- src/amplai_foundry/cli.py

# R21-2
sed -i '' '/^    unreadable = unreadable\[:limit\]$/d' src/amplai_foundry/cli.py
find . -name __pycache__ -prune -exec rm -rf {} +
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -o addopts="" -m "not slack_e2e" -q   # 1409 passed
git checkout -- src/amplai_foundry/cli.py
```

## Closing State

```text
$ git status --short
?? specs/003-slack-proposal-card/evidence/round-21-lens-contract.md
?? specs/003-slack-proposal-card/evidence/round-21-lens-failure.md
?? specs/003-slack-proposal-card/evidence/round-21-lens-regression.md

$ git diff --stat
(출력 없음)

$ 행별 hash 재계산
rows 97 | 어긋남 0 []
```

target 안의 파일은 하나도 안 썼다. 이 보고서는 target 밖의 새 파일이다.
