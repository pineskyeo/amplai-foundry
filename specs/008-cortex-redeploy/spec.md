# Spec — cortex 에 kit 을 재배포한다

`ALR-007`. `D-053` 이 남긴 "남은 것 1" 을 닫는다.

## WHY

`ALR-006` 이 kit 2.3.0 을 세 앱에 배포하려다 cortex 만 되돌렸다. 원인은 cortex 의 test
하나다.

```python
# cortex:tests/ai/test_loop_runtime_v2.py:895
#   class V22DoneContractTests — "§22 확장된 V2 Done 조건과 §23 .ai-team 비대화 금지"
def test_ai_team_has_no_new_top_level_directory(self):
    base = os.path.join(ROOT, ".ai-team")
    actual = {name for name in os.listdir(base) if os.path.isdir(...)}
    self.assertEqual(
        {"contracts", "evidence", "knowledge", "policy",
         "rules", "runtime", "verifiers"}, actual)
```

kit 이 `.ai-team` 최상위에 셋을 더해 이 test 가 깨진다.

| 경로 | 근거 | 생기는 조건 |
|---|---|---|
| `.ai-team/install/` | `install.py:29 STATE_REL` | 항상 |
| `.ai-team/backups/` | `install.py:30 BACKUP_ROOT_REL` | 기존 파일을 덮을 때. cortex 는 `.gitignore`·`.claude/settings.json`·SKILL.md 둘이 대상이라 생긴다 |
| `.ai-team/local/` | `install.py:580` | `--project-home` 을 줄 때. 배포 래퍼가 그 flag 를 넘긴다 (`kit_distribute.py:174`) |

`os.listdir` 로 파일시스템을 세므로 `.gitignore` 는 무관하다.

## 이 규약의 출처와 세 앱의 차이

이 test 는 오타 방지용이 아니다. cortex `specs/007-amplai-v21-doc-freshness-gardening/tasks.md:46`
이 "**`.ai-team` 비대화**" 를 §22 Done 조건으로 세웠고, cortex `.ai-team/README.md` 본문이
"`.ai-team`은 문서 내용도, Work별 report 이력도 쌓지 않는다. 정책과 schema만 둔다" 라고 적는다.

kit 이 넣는 셋 중 `backups/` 는 말 그대로 이력 누적이다. **규약이 막으려던 바로 그것이다.**

세 앱의 처리가 다르다. 실측이다.

| 앱 | `.ai-team` 최상위 규약 | 강제 수단 |
|---|---|---|
| cortex | 있다 — 정확히 일곱 | **test 로 강제** (`V22DoneContractTests`) |
| amplai-foundry | 같은 문장이 README 에 있다 | **없다.** 이식할 때 `V22DoneContractTests` 를 안 가져왔다. 대신 `D-051` 이 README 각주로 예외 셋(문서·이력·identity)을 명시 |
| synapse | 없다 | 없다. `.ai-team` 에 40개 넘는 디렉토리가 있다 |

**층이 둘이고 정본이 서로 다르다.** 이 구분이 "누가 기준인가" 를 정한다.

```text
.ai-team loop runtime V2   원산지 cortex.  D-046 이 cortex main:3a3eb46b 를 foundry 로 이식했다
AMPLAI Loop Kit             정본 amplai-foundry.  D-053
```

깨지는 것은 앞 층의 test 이고 깨뜨리는 것은 뒤 층이다.

## WHAT

**cortex 의 guard test 를 삭제하지 않고 완화한다.** kit 3개만 예외로 허용하고, 그 밖의 새
최상위 디렉토리는 계속 막는다.

```python
BASE = {"contracts", "evidence", "knowledge", "policy", "rules", "runtime", "verifiers"}
KIT  = {"install", "local", "backups"}   # AMPLAI Loop Kit 설치 중 (D-051 / D-053)
self.assertTrue(BASE <= actual)
self.assertEqual(set(), actual - BASE - KIT)
```

삭제가 아니라 완화를 고르는 이유는 둘이다.

1. **foundry 기준과 충돌하지 않는다.** foundry 는 그 자리에 test 가 없으므로 cortex 가
   완화된 test 를 갖는 것이 foundry 기준을 어기는 것이 아니다.
2. **`.ai-team` 우발적 비대화를 잡는 장치가 살아남는다.** 삭제하면 cortex README 의 규약
   문장은 남고 강제만 사라져 foundry 보다도 느슨해진다 — foundry 는 test 를 안 가지는 대신
   `D-051` 각주로 예외를 문서화했다.

그리고 **kit 쪽 결함 하나를 먼저 고쳐야 한다.** 아래 "선행 조건" 이다.

## 선행 조건 — kit test 하나가 세션 환경에 오염된다

`tests/ai/test_amplai_async_runtime.py:387`
(`AmplaiHookTest::test_session_hooks_inject_and_checkpoint_without_owning_work_state`)

test 는 temp repo 와 temp Store 로 격리해 놓고 `AMPLAI_PROJECT_HOME`·`AMPLAI_APP_ID` 를
지우지 않는다. 그 둘이 설정돼 있으면 `session_start` 가 **실제 Store** 를 읽고 단언이
깨진다.

```text
AssertionError: 'Next READY Work' not found in
  'AMPLAI Project State\nProject: ai-platform\nApp: synapse
   \nProject Store: /Users/pinesky/workspace/amplai-project ...'
```

**그 변수를 심는 것이 kit 자신이다.** `payload/scripts/amplai_hook.py:75-76` 의 SessionStart
hook 이 `CLAUDE_ENV_FILE` 로 세션에 export 한다.

즉 kit 이 설치된 저장소에서 Claude 세션으로 test 를 돌리면 이 test 가 **항상 실패한다.**
cortex 배포 후에는 그것이 `loop-runtime-tests` block check 를 통해 cortex 의 gate 전체를
FAIL 로 만든다.

이건 cortex 만의 문제가 아니라 **지금 foundry 에서도 재현된다.** 실측이다.

```text
tests/ai 만, 이 세션 shell (AMPLAI_* 설정됨)              FAILED 1건
tests/ai 만, env -u AMPLAI_PROJECT_HOME -u AMPLAI_APP_ID  82 passed
전 suite,   AMPLAI_* 설정됨                                1 failed, 1514 passed, 4 deselected
전 suite,   AMPLAI_* 지움                                  1515 passed, 4 deselected
```

두 값이 맞물린다 — `1514 + 1 = 1515`. 실패하는 test 는 하나이고 그것이 전부다.

env 민감 test 는 **정확히 이 하나다.** 나머지 1514 개는 영향 없다.

**이 저장소의 gate 가 지금 그것 때문에 FAIL 이다.** `verifier --profile v2` 의 block check
`pytest` 하나만 실패하고 나머지 열은 전부 PASS 다 (`ruff-check`·`ruff-format`·`mypy`·
`loop-runtime-doctor`·`manifest-validator`·`loop-shell-syntax`·`schema`·`vault-lint`·
`project-pack`·`kit-seal`). 앞 세션이 기록한 `pytest 1515 passed` 는 그 변수가 없는
shell 에서 잰 값이다 — 앞 세션이 틀린 게 아니라 **kit 을 설치한 세션에서 재는 순간
드러나는 결함이다.**

## Constraints

- cortex 저장소의 변경은 **그 저장소의 PR** 이다. 이 저장소에서 머지하지 않는다.
- `D-053` 을 낳은 `ALR-006` contract 의 `non_goals` 에 "cortex 와 synapse 의 저장소 규약을
  바꾸는 것" 이 있다. 이 Work 는 그것을 **의도적으로 뒤집는다** — 새 Decision 이 필요하다.
- cortex working tree 가 지금 dirty 다 (`--dry-run` 이 `dirty: ["cortex"]` 를 보고한다).
  배포 전에 그 저장소 상태를 정리한다. `--allow-dirty` 로 넘기지 않는다.
- kit 정본 변경은 버전을 올린다. `CHECKSUMS.sha256` 봉인과 `kit-seal` verifier check 가
  걸려 있다.

## Acceptance

| id | 조건 |
|---|---|
| `AC-001` | kit test 가 `AMPLAI_PROJECT_HOME`·`AMPLAI_APP_ID` 가 설정된 환경에서도 통과한다. 그 변수를 설정한 채와 지운 채 둘 다 실행해 확인한다 |
| `AC-002` | 회귀 test 가 있다 — 그 변수를 일부러 설정하고 hook test 가 통과하는 것을 강제한다. 제거하면 실패한다 |
| `AC-003` | kit 버전이 2.3.1 로 오르고 `CHECKSUMS.sha256` 이 실제 hash 와 일치한다. `selftest.py` 와 `kit-seal` verifier check 가 통과한다 |
| `AC-004` | foundry·synapse 가 2.3.1 로 갱신되고 `--verify` 가 일치를 보고한다 |
| `AC-005` | cortex 의 완화된 guard test 가 BASE 7개 부재를 여전히 FAIL 로 잡는다. 디렉토리 하나를 지워 확인한다 |
| `AC-006` | 완화된 guard test 가 kit 3개 밖의 새 최상위 디렉토리를 FAIL 로 잡는다. 임의 디렉토리를 만들어 확인한다 |
| `AC-007` | cortex 에 2.3.1 을 설치한 뒤 `tests/ai` 전체가 통과한다. Claude 세션 env 가 설정된 상태로도 통과한다 |
| `AC-008` | cortex `verifier --profile v2` 가 PASS, `loopctl.py doctor` 가 PASS |
| `AC-009` | `kit_distribute.py --verify` 가 세 앱 모두 일치를 보고하고 `rc=0` 이다. `A-F5`(cortex 미설치가 항상 mismatch) 가 해소된다 |
| `AC-010` | cortex `.ai-team/README.md` 에 예외 각주가 있다. foundry `D-051` 각주와 같은 형식이고 "kit 을 제거하면 이 각주도 함께 지운다" 를 포함한다 |

## Non-goals

- cortex 의 §22 다른 Done 조건 test 를 건드리는 것. `test_ai_team_has_no_new_top_level_directory`
  하나만 바꾼다.
- `.ai-team/install`·`local`·`backups` 를 다른 경로로 옮기는 것 (kit 2.4.0 안). 이 Work 는
  cortex 를 완화하는 쪽을 골랐다.
- synapse `tools/amplai-loop-kit/` 삭제 PR — `D-053` 의 "남은 것 2" 다. 별건이다.
- `policy.json` 제거 대칭 (`append_to_json_array` 의 역함수) — "남은 것 3" 이다.
- supervisor 를 켜는 것 — `D-052`·`D-053` Scope 와 같다.
- cortex 에 들어가는 죽은 path_rule (`tools/amplai-loop-kit/**` — cortex 에 그 경로가 없다).
  깨지지 않는다. synapse 삭제 건과 같은 부류라 그쪽에서 함께 본다.

## Evidence — 이 spec 의 근거는 전부 실측이다

cortex 원본을 건드리지 않고 `c7837d06` 복제본에 kit 2.3.0 을 실제 설치해 측정했다.

```text
설치 전   Ran 99 tests            OK
설치 후   Ran 181 tests           FAILED (failures=1, skipped=21)
          verifier --profile v2   FAIL — loop-runtime-tests 하나만
          doctor                  PASS
guard test 제거 후
          Ran 180 tests           OK (skipped=21)
          verifier --profile v2   PASS
          doctor                  PASS
```

**유일한 실패다.** 배제한 것들도 실측이다.

- skill surface test — kit 은 새 skill 디렉토리를 안 만들고 `.agents/skills/{work,design}/SKILL.md`
  를 `managed section merge` 로만 건드린다. `EXPECTED_SKILLS` 와 `user-invocable` 불변
- `.claude/skills` symlink mirror test — 무관
- policy test 전수 — kit 은 `forbidden_automatic_actions` 3건과 `path_rule` 1건을 append 하고,
  cortex 의 관련 단언은 전부 `assertIn`/subset 이다. equality 단언은 `public_commands`(불변)와
  kit 이 안 건드리는 목록들뿐
- `doc-freshness`·`repository-garden-integrity` 포함 나머지 check 전부 PASS

skip 21건은 설계된 것이다 — `"kit source is not vendored"` 15건,
`"kit source is not vendored beside the installed payload"` 6건. cortex 는 정본이 아니라
배포 대상이라 `tools/amplai-loop-kit/` 이 없다.

## 실험이 실제 Store 를 건드렸고 되돌렸다

`--project-home` 을 실제 Store 로 줘서 두 항목이 바뀌었다. 원상 복구했고 확인했다.

```text
supervisor/source.json           cortex → amplai-foundry 로 복구 (마지막 설치가 이기는 구조)
.amplai/local/apps/cortex.json   repo_path 를 복제본 → ~/workspace/cortex 로 복구 (재봉인)
--verify                          복구 전후 동일 — cortex installed: null (A-F5 로 알려진 상태)
foundry tree                      clean
cortex tree                       untracked 둘, 세션 시작 시점과 동일
```

다음에 같은 실험을 하려면 **`--project-home` 에 임시 Store 를 준다.** 실제 Store 를 주면
그 실험이 fleet 상태를 바꾼다.

## Documentation impact — 상속된 `STALE` 이었고 확인됐다

설계 시점에는 이랬다.

```text
docs validate --repo                      FRESH
docs impact specs/008-cortex-redeploy     STALE   (impacted 8건)
```

`loopv2.py:1383 work_base_commit()` 이 `merge-base HEAD origin/main` 을 쓰는데 그때
브랜치가 main 대비 41 commit 이라, **앞선 Work 들이 바꾼 파일까지 이 Work 의 변경으로**
돌아갔다. impacted 에 `docs/VISION.md`·`docs/ROADMAP.md` 처럼 cortex guard test 와 아무
관계 없는 것들이 들어 있었다.

**`PR #2` 가 머지된 뒤 다시 생성하니 `NOT_APPLICABLE`(impacted 0)이 나왔다.** 기준점이
정상화되자 사라진 것이므로 이 Work 가 만든 staleness 가 아니었다는 것이 확인됐다.
