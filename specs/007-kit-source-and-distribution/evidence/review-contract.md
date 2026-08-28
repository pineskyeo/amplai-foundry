# Review — Contract Lens (ALR-006)

- 대상: `specs/007-kit-source-and-distribution/`, commit 범위 `245fc37..5febffa`
- lens: contract — AC 전수, 수치 주장, `D-051`·`D-052`·`D-053` 준수, task manifest 정직성
- 실행 환경: `.venv/bin/python`, `PYTHONDONTWRITEBYTECODE=1`
- 이관 원본(`tools/amplai-loop-kit/` 의 upstream 내용)은 review 대상에서 뺐다

## Verdict

**blocker 있음 — P1 셋, Blocking-P2 셋.**

`completion.all_acceptance_passed: true` 는 정당하지 않다. AC-002 와 AC-014 가 실측으로
불통과이고 AC-015 는 manifest 자신이 미달을 적었다. 배포 자체(AC-006·AC-007·AC-010·
AC-012·AC-013)는 실측으로 튼튼하다 — 락은 세 저장소 사본과 Store 진입점 전부에서 닫혀
있고, 참조 전수표와 test 수치는 두 방법으로 재현된다.

## Acceptance 전수

| AC | 판정 | 확인 방법 |
|---|---|---|
| AC-001 | PASS (A-C1 첨부) | `git ls-tree -r 1c05001b` vs `HEAD` 대조 — 원본 39, 현재 43. `-SYNAPSE_INTEGRATION.md`, `+PROVENANCE.md`·`REFERENCE_INTEGRATION.md`·`distribution/targets.json`·`payload/store/supervisor/run`·`seal.py`, 내용 차이 12파일. 전부 2.3.0 이 의도한 것이나 `seal.py` 만 CHANGELOG 2.3.0 항목에 없다. `git cat-file -t 1c05001b` → commit 실재, `origin/main` 에 포함 |
| AC-002 | **FAIL (C-1)** | `selftest.py` → `ok: true`, check 10개 (PASS). `shasum -a 256 -c CHECKSUMS.sha256` → `payload/store/supervisor/run: FAILED`, 1행 불일치 |
| AC-003 | PASS | 원본을 `/tmp/kitorig` 에 `git archive` 로 꺼내 재계수. 테스트 제외 20건(라인합), `grep -roi` 로도 20 — 두 방법 일치. `D-053` Evidence 의 "56건 — 문서 20, fixture 36" 과 정확히 같다. 현재 15건(PROVENANCE 6, TEST_REPORT 7, targets.json 2)이고 셋 다 남은 이유가 적혀 있다 — README 첫 줄 `# AMPLAI Loop Kit 2.3.0`, `REFERENCE_INTEGRATION.md` 0건, INSTALLATION·QUICKSTART 0건 |
| AC-004 | PASS | `README.md:28-38` 이 `required: false` 셋을 표로 설명한다. 세 앱 실측 — handoff SKILL 둘은 synapse 만 있음, `AUTONOMY_POLICY.md` 는 `create_if_missing: true` 라 설치 후 셋 다 있음 |
| AC-005 | PASS (A-C5·C-2 첨부) | `~/workspace/amplai-project/supervisor/` 에 `run`·`VERSION`·`source.json`, `.amplai/locks/` 존재. `--project-home` 없이 설치하면 앱 설치는 진행되고 `project_store: null` — 건너뛰지만 reason 문자열이 없다 |
| AC-006 | PASS | 실측. Store 락을 잡은 채 `foundry/cortex/synapse` 사본과 Store `run` 을 `--once` 로 띄워 넷 다 `exit=3`, `--dry-run` `exit=0`. `git check-ignore` → `.amplai/locks/` 가 Store `.gitignore:3` 에 걸린다. stale 회수는 `tests/ai/test_amplai_kit_regressions.py` 의 락 test 12개 통과로 확인 |
| AC-007 | PASS | 위와 같은 실행. **다른 저장소 사본(cortex·synapse)도 같은 Store 락에 걸려 `exit=3`** — `D-052` 가 한계로 적은 "앱 repo 사본을 직접 실행하면 Store 락을 안 잡는다" 가 닫혔다 |
| AC-008 | PASS | `targets.json` 에 `/`·`~` 로 시작하는 값 0. `git check-ignore -v` → `.gitignore:22 .ai-team/local/`. 그 줄은 kit 의 `AMPLAI-LOCAL-BEGIN/END` 블록 안에 하나만 있고 중복 없음 |
| AC-009 | PASS | `kit_distribute.py --dry-run` → `ok: true`, 세 대상 전부 계획 출력, 세 저장소 git status 무변화(설치 후라 actions 0). 실패 경로는 `tests/test_kit_distribute.py` 의 `test_a_failed_plan_installs_nothing`·`test_an_unresolved_target_stops_the_run`·`test_a_failed_selftest_stops_before_any_plan` 이 덮는다 |
| AC-010 | PASS (C-2 첨부) | 세 앱 `.ai-team/install/amplai-loop-kit.json` → `package_version: 2.3.0` 셋 다. synapse 는 2.2.0 이 있던 자리라 갱신 경로 |
| AC-011 | PASS | `tests/test_kit_distribute.py:186 test_an_install_failure_stops_and_reports_what_was_done` 이 성공/실패 구분과 앞선 앱 비되돌림을 확인한다 |
| AC-012 | PASS | `kit_distribute.py --verify` → `ok: true`, `mismatched: []`, 세 앱 `matches: true`. 불일치 지목은 `test_a_version_mismatch_names_the_app`·`test_a_target_with_no_install_record_counts_as_mismatched` 가 덮고, `test_installed_version_reads_the_field_the_installer_writes` 가 필드 결함 회귀를 막는다 |
| AC-013 | PASS | synapse `origin/main` 에서 두 방법 재계수 — 경로 `tools/amplai-loop-kit` 4건/3파일, 이름 `amplai-loop-kit` 5파일. `KIT-DISTRIBUTION.md:122-124` 의 수치와 정확히 같고 표 5행이 방법 2의 5파일과 대응한다. `policy.json:156` 도 실측 일치 |
| AC-014 | **FAIL (C-3)** | amplai-foundry — doctor PASS, verifier v2 PASS, pytest `1494 passed, 4 deselected`. synapse — kit test `54 passed, 10 skipped`, 나머지 2건 실패는 knowledge plane claim 문제로 이 배포와 무관함을 실측 확인. **cortex — `tests/ai/test_loop_runtime_v2.py::V22DoneContractTests::test_ai_team_has_no_new_top_level_directory` 가 이 배포 때문에 실패한다** |
| AC-015 | PARTIAL | 245fc37 복제본에 설치 후 `--uninstall` → 잔여 2건(`M .ai-team/runtime/policy.json`, `?? .ai-team/backups/`). policy.json 은 `json.load` 비교로 원본과 **의미 동일**을 확인했고 `python -m json.tool` diff 도 0 — manifest 의 서술이 정확하다. 제거 출력의 note 가 한계를 알린다. Store 복제본에서 `supervisor/` 제거 후 `amplai.py project verify` → `ok: true`. 다만 contract 문구 "git status 가 비는 것" 은 충족되지 않는다 |

## 수치 주장 검산

| 주장 | 출처 | 실측 | 판정 |
|---|---|---|---|
| test 1494 = 1409 +20 +64 +1 | 5febffa commit message | 실행 `1494 passed, 4 deselected` / collect 합 1494 — 두 방법 일치. 신규 test 파일은 넷뿐이고 기존 test 파일 수정 0(`git diff --stat 245fc37..HEAD -- tests/`). `test_kit_distribute.py` 21, `tests/ai/` 64(16+10+38), 합 85. 1494-85 = 1409 | 합계 정확. 다만 래퍼는 20 이 아니라 21 이고 "+1 회귀 test" 가 그 안에 있다 |
| synapse 언급 26 → 13 | `evidence-trace.jsonl` seq 3 | 13 은 재현된다(e071f6f 시점 PROVENANCE 6 + TEST_REPORT 7). **26 은 어떤 계수법으로도 재현하지 못했다** — 원본은 문서 20 / 전체 56 이고, contract AC-003 과 `D-053` Evidence 둘 다 20 과 56 을 적는다 | **C-6** |
| dry-run action 29/29/31 | `evidence-trace.jsonl` seq 3 | 재현 불가(설치 완료 후라 전부 0) | A-C3 |
| dry-run action 28/31/29 | `KIT-DISTRIBUTION.md:53-55`, seq 5, 10f64f5 | 같은 이유로 재현 불가. synapse 31·cortex 29 는 두 자리가 같고 **amplai-foundry 만 29 → 28 로 다르다**. 이유가 어디에도 없다 | A-C3 |
| 29/32/30 | — | 이 저장소 어디에서도 찾지 못했다. 실재하는 두 값은 29/29/31(seq 3)과 28/31/29(문서·seq 5)다 | 확인 못 함 |
| synapse kit test 54 passed | 5febffa commit message | `pytest tests/ai/test_amplai_{async_runtime,kit_installer,kit_regressions}.py` → `54 passed, 10 skipped` | 정확 |
| synapse 2건 실패는 무관 | 5febffa commit message | 두 실패 모두 cortex 소스 경로를 가리키는 knowledge claim 문제(`stale_evidence_path`, `duplicate_claim_id`) — kit 과 무관 | 정확 |
| 기준선 1409 → 1494 | `CURRENT_ITEM.md`, commit | 위 계산으로 확인 | 정확 |
| synapse 참조 4건/3파일, 5파일 | `KIT-DISTRIBUTION.md:122-124` | 두 방법 재계수 일치 | 정확 |

## Decision 준수

**`D-051` 셋.**

- (a) ruff 예외 — `pyproject.toml:56-66` 의 `extend-exclude` 는 `tools/amplai-loop-kit`, `scripts/amplai{,_hook,_runtime,_supervisor}.py`, `tests/ai` 뿐이다. 넷 다 kit 정본이거나 kit 이 소유한 설치물이다(`manifest.json:173-183` 이 `tests/ai/*` 를 `owned_files` 로 선언). `src/` 는 손대지 않았고 `tests/` 도 `tests/ai` 를 뺀 나머지는 strict 다. **실질은 지켜졌다.** 다만 같은 파일의 주석 하나가 이제 부정확하다(A-C4)
- (b) `.ai-team/README.md` 각주 — `git diff --numstat 245fc37..HEAD` → `30 0`. **삭제 줄 0, 본문 무수정**이 확인된다. 각주 둘 다 `D-051`·`D-053` 을 인용하고 "kit 을 제거하면 이 각주도 함께 지운다" 를 적는다
- (c) test 기준선 — 위 수치표대로 정확하다

**`D-052`.** 락 부분은 실측으로 닫혔다(AC-006·AC-007). Store 구조는 결정문과 다르다 — **C-5**.

**`D-053`.** 정본 위치·PROVENANCE·2.3.0 구성·두 층 배포 경로 넷 다 구현과 맞는다.
`D-053` 본문에 "Store 에 둘 것은 run 진입점과 VERSION 뿐" 이라는 문장은 **없다** — 그
서술은 `install.py:902-905` docstring 과 `KIT-DISTRIBUTION.md:64-66` 에 있고, 후자는
`source.json` 을 명시한다. 즉 `source.json` 은 문서와 구현이 어긋나지 않는다.
어긋나는 것은 `D-052` 쪽이다(C-5).

## PROVENANCE 검증

- 출처 commit `1c05001b7e82098de230b8e8b961ee8206ca7169` 실재. `origin/main` 에 포함되고
  메시지·머지 시각(`2026-08-28T04:29:44Z`)이 PROVENANCE 기재와 일치한다
- "이관 시점에 파일 목록이 원본과 정확히 같았다" — **지금은 검증할 수 없다.** 순수 이관
  상태의 commit 이 없고, 첫 commit `e071f6f` 가 T001 과 T002 를 함께 담아 그 시점에
  이미 `SYNAPSE_INTEGRATION.md` → `REFERENCE_INTEGRATION.md` 이름 변경이 들어 있다.
  주장이 틀렸다는 근거는 없고 재현 경로가 없을 뿐이다
- "그 뒤 이 파일과 `VERSION`(2.3.0)이 더해졌다" — `VERSION` 은 원본에 이미 있었다(A-C2)

## Blocker

| id | 등급 | 위치 | 내용 |
|---|---|---|---|
| C-1 | P1 | `tools/amplai-loop-kit/CHECKSUMS.sha256:36` | 봉인이 깨져 있다. `payload/store/supervisor/run` 의 기록값 `b097ab89…` 이 실제 `5339 7739…` 와 다르다. 5febffa 가 그 파일을 고치고 `seal.py` 를 다시 안 돌렸다. `selftest.py`·`install.py` 어디도 `CHECKSUMS` 를 읽지 않아(`grep CHECKSUMS` 0건) 검사에 안 걸린다. AC-002 불통과 |
| C-2 | P1 | `~/workspace/amplai-project/supervisor/run:71` | 배포된 Store 진입점이 **수정 전 코드**다 — `read_json(record).get("version")`. 정본(`payload/store/supervisor/run:71`)은 `package_version` 으로 고쳐졌지만 Store 에 재배포되지 않았다. 결과로 `app_version` 이 `None` 이 되고 `run:116` 의 `if store_version and app_version and …` 가 통째로 건너뛰어진다. **`D-052` 가 "불일치면 fail-closed 한다" 로 잡아 둔 버전 드리프트 방어가 지금 살아 있는 Store 에서 무력화돼 있다** |
| C-3 | P1 | `~/workspace/cortex/tests/ai/test_loop_runtime_v2.py:901` | cortex 배포가 그 저장소의 규약 test 를 깨뜨렸다. `V22DoneContractTests::test_ai_team_has_no_new_top_level_directory` 가 `.ai-team` 아래 `install`·`local`·`backups` 셋을 새로 보고 실패한다(cortex `HEAD` 는 7개만 추적). contract `non_goals` 의 "cortex 와 synapse 의 저장소 규약을 바꾸는 것" 과 AC-014 를 둘 다 어긴다. 5febffa 는 "cortex doctor PASS" 만 적고 이 실패를 적지 않았다 |
| C-4 | Blocking-P2 | `specs/007-kit-source-and-distribution/task-manifests/index.yaml:64-66` | manifest 집합이 자기모순이다. `index.yaml` 은 T006 을 `status: blocked`, 나머지 여섯을 `status: ready` 로 두는데 per-task manifest 는 일곱 다 `status: done` 이다. 생성물 `tasks.md:63,67` 도 "Blocked — DO NOT EXECUTE" 와 미체크 상태 그대로이고 OQ-01(Store 경로)·OQ-02(git_publish)도 해소됐는데 열려 있다. `all_acceptance_passed: true` 를 뒷받침해야 할 index 가 반대를 말한다 |
| C-5 | Blocking-P2 | `docs/workstreams/messenger-governance-closure-v3/DECISIONS.md:1811-1826` vs `tools/amplai-loop-kit/install.py:899-936` | `D-052` 는 "**코드도 Store 에 두고**" 를 결정문에 적고 `supervisor/` 에 `amplai_supervisor.py`·`amplai_runtime.py`·`VERSION`·`run` 넷을 두는 구조를 그렸다. 구현은 `run`·`VERSION`·`source.json` 셋만 두고 supervisor 는 앱 사본에서 돌린다. `D-053` 의 amend 는 락 얘기만 하고 이 변경을 기록하지 않았다. 결정 문서와 구현이 어긋난 채 승인된 상태로 남아 있다 |
| C-6 | Blocking-P2 | `specs/007-kit-source-and-distribution/evidence-trace.jsonl:3` | `"synapse_mentions": "26 → 13"` 의 26 을 재현하지 못했다. 원본 실측은 문서 20 / 전체 56 이고 contract AC-003("이관 전 20건이 기준이다")과 `D-053` Evidence("56건 — 문서 20, 테스트 fixture 36")가 둘 다 그 값을 적는다. 26 은 세 기준 어느 것과도 맞지 않는다. provenance 기록이 근거 없는 수를 담고 있다 |

## Verified Clean

- 단일 인스턴스 락이 실환경에서 닫혀 있다 — Store 락 보유 중 foundry·cortex·synapse 사본과
  Store `run` 넷 모두 `exit=3`, `--dry-run` 만 `exit=0`. `D-052` 가 한계로 인정한 우회
  경로가 실제로 사라졌다
- test 수 1494 와 그 증가분 85 가 두 방법으로 일치한다
- `D-051`(a)(b)(c) 셋 다 실질 충족. 특히 `.ai-team/README.md` 는 추가 30 / 삭제 0 이라
  "본문 무수정" 주장이 정확하다
- AC-013 참조 전수표가 두 방법으로 정확히 재현된다(4건/3파일, 5파일). `policy.json:156`
  줄 번호까지 맞다
- AC-008 두 층 경로 분리 — 커밋본에 절대경로 0, host-local 파일이 무시되고 kit 이 넣는
  `.ai-team/local/` 줄과 중복도 없다
- 제거 절차의 `policy.json` 한계 서술이 정확하다 — 파싱 비교로 내용 동일을 확인했고
  제거 출력의 note 가 그 사실을 알린다
- `targets.json`·`kit_distribute.py` 의 계약이 test 로 고정돼 있다(21개, 절대경로 금지와
  gitignore 여부까지 test 가 확인)

## Advisory

| id | 위치 | 내용 |
|---|---|---|
| A-C1 | `tools/amplai-loop-kit/CHANGELOG.md:3` | 2.3.0 이 아직 `(in progress)` 다. 세 앱 배포까지 끝난 상태와 안 맞는다. 또 2.3.0 항목이 `seal.py` 추가를 적지 않아 AC-001 의 "바뀐 파일 목록이 2.3.0 변경 목록과 정확히 일치" 를 문서만으로는 만족시키지 못한다 |
| A-C2 | `tools/amplai-loop-kit/PROVENANCE.md:26` | "그 뒤 이 파일과 `VERSION`(2.3.0)이 더해졌다" — `VERSION` 은 원본 39파일에 이미 있었고 값만 2.2.0 → 2.3.0 으로 바뀌었다. "더해졌다" 가 아니다 |
| A-C3 | `docs/workstreams/amplai-loop-runtime-adoption/KIT-DISTRIBUTION.md:53` vs `evidence-trace.jsonl:3` | amplai-foundry dry-run action 수가 29(seq 3)와 28(문서·seq 5)로 다르다. synapse·cortex 는 두 자리가 같다. 줄어든 이유가 어디에도 없다. 설치 완료 후라 지금은 재현할 수 없다 |
| A-C4 | `pyproject.toml:74` | "이 예외는 `scripts/` 의 두 파일에만 걸린다. `src/` 와 `tests/` 는 그대로 strict 다" — 같은 파일 line 64 가 `tests/ai` 를 `extend-exclude` 에 넣었으므로 뒷문장이 이제 부정확하다. 5febffa 의 "src/ 와 tests/ 는 strict 그대로다" 도 같다. 실질(kit 소유 경로만 예외)은 맞으므로 문구만 고치면 된다 |
| A-C5 | `tools/amplai-loop-kit/install.py:852` | `--project-home` 이 없으면 `configure_project_store` 가 `None` 을 돌려 보고가 `project_store: null` 뿐이다. AC-005 의 "건너뛰고 보고한다" 를 만족할 reason 이 없다. 같은 함수의 다른 분기는 `{"installed": false, "reason": …}` 형식을 쓰므로 그 형식을 재사용하면 된다 |
| A-C6 | `specs/007-kit-source-and-distribution/evidence-trace.jsonl` | `git_checkpoint` 가 전 항목에서 한 commit 씩 뒤쳐진다 — seq 3 은 `245fc37` 을 적지만 그 slice 의 내용은 `e071f6f` 에 있고 timestamp 도 `e071f6f` 의 commit 시각과 같다. seq 4·5·7 도 동일하다. HEAD `5febffa` 를 가리키는 항목은 하나도 없다 |
| A-C7 | `~/workspace/amplai-project/.amplai/locks/supervisor.lock/owner.json` | T006 evidence 실행의 잔여 락이 남아 있다(pid 82198, `2026-08-28T05:52:38Z`). stale 회수로 풀리지만 정리되지 않은 흔적이다 |
| A-C8 | `~/workspace/amplai-project` | Store 가 git repo 로 초기화됐지만 commit 이 하나도 없다 — `git status` 가 `project.json`·`apps/`·`policy.json` 을 전부 `??` 로 낸다. `D-052` 는 Store 를 git repo 로 정의하고 event chain 을 전제한다 |

## Not Checked

- kit 이관 원본(upstream 2.2.0)의 내용 자체 — 지시로 제외
- cortex·synapse 저장소의 `doctor` 를 이 review 에서 재실행하지 않았다. 두 저장소에 쓰기를
  하지 않기 위해서다. 대신 두 저장소의 `tests/ai` 를 `PYTHONDONTWRITEBYTECODE=1`
  `-p no:cacheprovider` 로만 돌렸다. 5febffa 의 "synapse doctor PASS / cortex doctor PASS"
  주장은 **확인 못 함**
- AC-009·AC-011 의 "대상 하나를 일부러 깨뜨리는" 실측은 다른 저장소를 건드려야 해서
  하지 않았다. `tests/test_kit_distribute.py` 의 해당 test 통과로 대신했다
- `29/32/30` 이라는 dry-run 수치의 출처 — 저장소 전체에서 찾지 못했다
- AC-001 의 "이관 시점" 파일 목록 동일성 — 순수 이관 commit 이 없어 재현 경로가 없다
