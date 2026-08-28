# Plan — cortex 에 kit 을 재배포한다

`ALR-007`. spec 은 [spec.md](spec.md) 다.

## 경계 — 저장소 셋을 건드린다

```text
amplai-foundry   kit 정본 수정 (2.3.0 → 2.3.1). 이 저장소의 commit
synapse          설치 갱신만. 저장소 규약 무변경
cortex           test 완화 + README 각주 + 설치.  그 저장소의 PR 이 따로 필요하다
```

**cortex PR 은 이 저장소에서 머지하지 않는다.** `/work` 는 브랜치와 commit 까지 만들고
push·PR·머지는 사람이 정한다.

## 순서가 강제된다

`AC-001` 이 `AC-007` 의 선행이다. kit test 가 세션 env 에 오염된 채로 cortex 에 설치하면
cortex 의 `loop-runtime-tests` block check 가 **Claude 세션 안에서 항상 FAIL** 한다. 그
상태로 배포하면 cortex 의 gate 를 우리가 깨뜨린 것이 된다.

```text
S01  kit hook test 를 격리한다        foundry
S02  kit 2.3.1 로 올리고 재봉인한다    foundry
S03  foundry·synapse 를 2.3.1 로       foundry + synapse 설치 산출물
S04  cortex guard test 완화 + 각주      cortex 브랜치
S05  cortex 에 2.3.1 설치              cortex 브랜치
S06  --verify 를 정상화한다 (A-F5)     foundry
```

S04 는 S01~S03 과 독립이라 병행 가능하지만, **S05 는 S03·S04 둘 다를 기다린다.**

## Slice

### S01 — kit hook test 를 세션 환경에서 격리한다

대상: `tools/amplai-loop-kit/payload/tests/ai/test_amplai_async_runtime.py`
(설치본은 `tests/ai/test_amplai_async_runtime.py`)

`AmplaiHookTest.setUp` 이 `AMPLAI_PROJECT_HOME`·`AMPLAI_APP_ID` 를 지우고 `tearDown` 이
되돌린다. test 가 이미 `os.environ.copy()` 로 복원하는 구간을 갖고 있으므로 그 안에서
`os.environ.pop` 을 하는 것으로 충분하다.

**`session_start` 구현 쪽은 안 고친다.** env 로 Store 를 찾는 것은 hook 의 의도된 동작이다
(`amplai_hook.py:75-76` 이 그 변수를 쓰라고 심는다). 고칠 것은 test 의 격리다.

회귀 test(`AC-002`): 그 변수를 **일부러 실제 Store 경로로 설정한 채** hook test 를 돌려
통과하는 것을 강제한다. `payload/tests/ai/test_amplai_kit_regressions.py` 에 넣는다 — 그 파일이
"코드는 맞는데 test 가 못 잡았다" 부류를 모으는 자리다.

검증:

```bash
.venv/bin/python -m pytest tests/ai -q                                   # env 설정된 채
env -u AMPLAI_PROJECT_HOME -u AMPLAI_APP_ID .venv/bin/python -m pytest tests/ai -q
```

둘 다 통과해야 한다. 지금은 앞의 것이 1건 실패한다.

### S02 — 2.3.1 로 올리고 재봉인한다

```text
VERSION                2.3.0 → 2.3.1
manifest.json          version 과 owned_files hash
CHECKSUMS.sha256       재생성
PROVENANCE.md          2.3.1 이 무엇을 고쳤는지 한 줄
```

검증: `python3 tools/amplai-loop-kit/selftest.py`,
`.venv/bin/python .ai-team/verifiers/run.py --profile v2` 의 `kit-seal` check.

**봉인이 깨진 채 commit 된 적이 두 번 있다** (`ALR-006` review). `kit-seal` 이 block 이므로
이제 자동으로 잡힌다. 그래도 commit 전에 한 번 돌린다.

### S03 — foundry·synapse 를 2.3.1 로 갱신한다

```bash
.venv/bin/python scripts/kit_distribute.py --dry-run
.venv/bin/python scripts/kit_distribute.py --app amplai-foundry --project-home ~/workspace/amplai-project
.venv/bin/python scripts/kit_distribute.py --app synapse --project-home ~/workspace/amplai-project
.venv/bin/python scripts/kit_distribute.py --verify
```

**`--all` 과 `--app` 을 섞지 않는다.** `A-F1` 이 그 조합은 경고 없이 `--app` 만 처리한다고
기록했다.

`--project-home` 을 주면 `supervisor/source.json` 이 **마지막 설치한 앱** 으로 바뀐다.
설치 순서를 정해 두고 끝나면 `source.json` 을 확인한다.

synapse 는 별도 저장소라 그 tree 에 변경이 생긴다. dirty gate 를 넘기지 말고 그 저장소
상태를 먼저 본다.

### S04 — cortex guard test 를 완화하고 각주를 단다

cortex 브랜치 하나. 파일 둘이다.

```python
# tests/ai/test_loop_runtime_v2.py — V22DoneContractTests
def test_ai_team_has_no_new_top_level_directory(self):
    """§23 .ai-team 비대화 금지. kit 설치 중에는 예외 셋을 허용한다 (D-051/D-053)."""
    base = os.path.join(ROOT, ".ai-team")
    actual = {
        name for name in os.listdir(base)
        if os.path.isdir(os.path.join(base, name))
    }
    core = {"contracts", "evidence", "knowledge", "policy",
            "rules", "runtime", "verifiers"}
    kit = {"install", "local", "backups"}
    self.assertTrue(core <= actual, core - actual)
    self.assertEqual(set(), actual - core - kit)
```

`.ai-team/README.md` 의 Directory ownership 절 아래에 각주를 단다. foundry `D-051` 각주와
같은 형식이고 **"kit 을 제거하면 이 각주도 함께 지운다"** 를 포함한다.

검증(`AC-005`·`AC-006`): 둘 다 실제로 만들어 FAIL 을 본다.

```text
core 하나 제거      .ai-team/rules 를 임시로 옮기고 FAIL 확인
kit 밖 디렉토리     .ai-team/zzz 를 만들고 FAIL 확인
```

**이 둘을 실행으로 확인하지 않으면 S04 는 안 끝난다.** `assertEqual` 을 두 단언으로 쪼개면
한쪽이 무력화돼도 다른 쪽이 통과할 수 있다 — `ALR-006` regression lens 가 잡은 부류다.

### S05 — cortex 에 2.3.1 을 설치한다

전제: cortex working tree 가 clean 이고 S04 브랜치 위다.

```bash
.venv/bin/python scripts/kit_distribute.py --dry-run --app cortex
.venv/bin/python scripts/kit_distribute.py --app cortex --project-home ~/workspace/amplai-project
```

검증(`AC-007`·`AC-008`), cortex 저장소에서:

```bash
python3 -m unittest discover -s tests/ai -t .        # env 설정된 채로도
python3 .ai-team/verifiers/run.py --profile v2
python3 scripts/loopctl.py doctor
```

기대값 — 복제본 실측 기준. 2.3.1 이 test 를 하나 더할 수 있으므로 수는 **세어서** 적는다.

```text
tests/ai   180+ tests OK (skipped 21)
verifier   PASS
doctor     PASS
```

### S06 — `--verify` 를 정상화한다 (`A-F5`)

cortex 가 설치되면 `--verify` 는 저절로 `rc=0` 이 된다. 그래도 `A-F5` 의 본질 — "의도적으로
배포하지 않음" 을 표현할 수단이 없다 — 은 남는다.

`distribution/targets.json` 의 target 에 `expect_installed`(기본 `true`)를 더하고 `--verify`
가 그것을 존중한다. cortex 항목의 note 는 **사유를 과거형으로 고쳐 남긴다** — 지우면 왜
이런 구조가 됐는지가 사라진다.

이 slice 는 S05 와 독립이고, S05 를 못 하게 되더라도 단독으로 값이 있다.

## Failure path

| 실패 | 대응 |
|---|---|
| S01 이 다른 env 민감 test 를 더 드러낸다 | 전부 같은 방식으로 격리한다. 수가 3을 넘으면 개별 격리 대신 `setUpModule` 로 올린다 |
| S03 에서 synapse 갱신이 dirty gate 에 막힌다 | 그 저장소 상태를 먼저 정리한다. `--allow-dirty` 를 쓰지 않는다 |
| S05 배포가 중간에 실패한다 | `kit_distribute.py` 가 실패 지점에서 멈추고 어디까지 됐는지 보고한다. 그 보고를 근거로 `--uninstall --app cortex` 로 되돌린다 |
| cortex 에서 예상 밖 test 가 깨진다 | 복제본에서 재현하고 원인을 kit / cortex 규약 중 어디에 속하는지 먼저 가른다. cortex 규약이면 **다시 Decision 이다** — 임의로 고치지 않는다 |
| `--project-home` 을 실제 Store 로 준 실험이 fleet 상태를 바꾼다 | 실험에는 임시 Store 를 쓴다. 실제 Store 를 썼으면 `supervisor/source.json` 과 `.amplai/local/apps/*.json` 을 확인해 복구한다 |

## Rollback

```text
S01·S02   commit revert. 설치본은 S03 재실행으로 되돌아간다
S03       kit_distribute.py 로 2.3.0 을 다시 설치. 정본을 되돌린 뒤 재배포한다
S04       cortex 브랜치를 버린다. 머지 전이면 비용 0
S05       kit_distribute.py --uninstall --app cortex.  ALR-006 이 이미 한 번 완주한 경로다
```

**`--uninstall` 이 완전하지 않은 것이 둘 알려져 있다.** `policy.json` 표기 차이(내용은 동일)와
`.ai-team/backups/`(의도적으로 남긴다). cortex 를 되돌릴 때 `backups/` 가 남으면 완화된
test 는 그것을 허용하므로 이번에는 문제가 안 된다 — 그 자체가 완화안의 부수 효과다.

## Validation

```text
foundry    .venv/bin/python -m pytest
           .venv/bin/python .ai-team/verifiers/run.py --profile v2   (kit-seal 포함)
           .venv/bin/python scripts/loopctl.py doctor
           .venv/bin/python scripts/kit_distribute.py --verify
cortex     python3 -m unittest discover -s tests/ai -t .
           python3 .ai-team/verifiers/run.py --profile v2
           python3 scripts/loopctl.py doctor
synapse    그 저장소의 doctor 와 kit test
```

**test 수는 세어서 적는다.** `ALR-006` 이 두 방법으로 세는 규율을 남겼다 (래퍼 + kit tests).

**foundry test 는 `.venv/bin/python` 으로 돌린다.** 맨 `python3` 는 system 3.9 라
`tomllib` 부재로 가짜 실패를 낸다. cortex 는 자기 저장소 관행대로 `python3` 를 쓴다 —
복제본에서 3.9 로 181 tests 가 도는 것을 확인했다.

## Decision impact

새 Decision 이 하나 필요하다 — `ALR-006` contract 의 `non_goals` "cortex 와 synapse 의
저장소 규약을 바꾸는 것" 을 이 Work 가 **의도적으로 뒤집는다.**

```text
D-054   cortex 의 .ai-team 최상위 규약을 kit 예외 셋으로 완화한다
        docs/workstreams/messenger-governance-closure-v3/DECISIONS.md 에 append
        .ai-team/knowledge/decisions.index.json 에 등재
```

`D-051`·`D-053` 은 뒤집히지 않는다 — 이 Decision 이 그 둘의 후속이다.

갱신 대상 문서:

```text
docs/workstreams/amplai-loop-runtime-adoption/KIT-DISTRIBUTION.md   "cortex 를 제거한 이유" 절
docs/workstreams/amplai-loop-runtime-adoption/CURRENT_ITEM.md       "남은 것" 1
tools/amplai-loop-kit/distribution/targets.json                     cortex note
cortex:.ai-team/README.md                                           예외 각주 (그 저장소)
```

`docs impact` 와 `docs validate --repo` 로 기계적으로 재확인한다.
