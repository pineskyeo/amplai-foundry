# Kit 배포와 제거 — 운영 문서

`D-053` 이 정한 구조의 운영 절차다. 정본은 이 저장소의 `tools/amplai-loop-kit/` 이고
`ALR-006` 이 만들었다.

```text
정본        amplai-foundry:tools/amplai-loop-kit/   (2.3.0)
배포 대상   amplai-foundry(source), synapse, cortex
배포 명령   scripts/kit_distribute.py
```

## 현재 설치 상태 (2026-08-28)

```text
amplai-foundry   2.3.0   설치됨
synapse          2.3.0   설치됨
cortex           미설치   배포했다가 제거했다 — 아래 참조
```

### cortex 를 제거한 이유

cortex 에는 `.ai-team` 최상위 디렉토리를 **정확히 일곱으로 못박은 test** 가 있다.

```python
# cortex:tests/ai/test_loop_runtime_v2.py — test_ai_team_has_no_new_top_level_directory
self.assertEqual(
    {"contracts", "evidence", "knowledge", "policy", "rules", "runtime", "verifiers"},
    actual)
```

kit 이 `install`·`local`·`backups` 셋을 더해 이 test 가 깨졌다. **세 앱 중 cortex 에만
있는 규약이다** — synapse 의 `.ai-team` 에는 이미 40개 넘는 디렉토리가 있고 그 test 가
없으며, 이 저장소는 `D-051` 이 각주로 처리했다.

그 test 를 고치는 것은 `ALR-006` contract 의 `non_goals`(대상 저장소의 규약을 바꾸는 것)가
막는다. **cortex 가 규약을 갱신하면 다시 배포한다.** `targets.json` 의 항목은 그대로 두어
근거를 남겼다.

제거는 완전했다 — `git status` 에 kit 흔적 0, `tests/ai` 99 passed, `doctor` PASS.

## 경로 설정 — 두 층

절대경로는 커밋하지 않는다. kit 자신이 `apps/<id>.json`(커밋)과
`.amplai/local/apps/<id>.json`(host-local)을 나누는 것과 같은 이유다.

```text
tools/amplai-loop-kit/distribution/targets.json   커밋. app_id·project_id·role·path_hint
.ai-team/local/kit-targets.json                   host-local. 실제 절대경로 (gitignore)
```

`path_hint` 는 힌트일 뿐이고 권위가 없다. **경로를 못 정하면 배포기는 추측하지 않고
멈춘다** — 후보를 보여 주고 사람이 `kit-targets.json` 에 적어야 진행한다.

`.gitignore` 는 kit 의 marker 블록(`# AMPLAI-LOCAL-BEGIN`)을 그대로 쓴다. 직접 한 줄
적으면 설치 때 kit 이 또 넣어 중복되지만, 같은 marker 를 쓰면 kit 이 그 블록을 관리한다.

## 배포

```bash
.venv/bin/python scripts/kit_distribute.py --dry-run          # 계획만. 아무것도 안 바꾼다
.venv/bin/python scripts/kit_distribute.py --all              # 전체 배포
.venv/bin/python scripts/kit_distribute.py --app cortex       # 하나만
.venv/bin/python scripts/kit_distribute.py --verify           # 설치 버전 대조
.venv/bin/python scripts/kit_distribute.py --uninstall --app cortex
```

순서와 게이트는 이렇다.

```text
1. preflight        selftest, 대상 경로 해석, working tree 상태
2. 전체 dry-run     하나라도 실패하면 아무것도 설치하지 않는다
3. 순차 설치        source 를 먼저. 실패 지점에서 멈추고 어디까지 됐는지 보고한다
4. 사후 검증        각 대상의 install record 버전을 kit VERSION 과 대조
```

**dry-run 은 uncommitted 변경이 있어도 돈다** — 계획은 아무것도 쓰지 않기 때문이다.
실제 설치만 막고, 필요하면 `--allow-dirty` 로 넘긴다.

`--dry-run` 실측 (2.3.0):

```text
amplai-foundry   28 actions
synapse          31 actions      handoff skill 둘이 더 있어 +2 다
cortex           29 actions
```

## Store 와 supervisor

`--project-home` 을 주면 Store 를 만들고 `<PROJECT_HOME>/supervisor/` 에 진입점을 놓는다.
Store 가 없으면 그 단계를 건너뛰고 보고한다 — 앱 설치는 진행된다.

```text
<PROJECT_HOME>/supervisor/run           진입점. 어느 앱 사본을 실행할지 정하고 버전을 대조한다
<PROJECT_HOME>/supervisor/VERSION       설치된 kit 버전
<PROJECT_HOME>/supervisor/source.json   실행할 앱 (마지막 설치가 이긴다. --source-app 로 덮는다)
<PROJECT_HOME>/.amplai/locks/supervisor.lock
```

**supervisor 는 하나만 산다.** 락이 `amplai_supervisor.py` 안에 있으므로 Store 진입점을
쓰든 앱 사본을 직접 실행하든 두 번째는 거부된다. 실측이다.

```text
두 번째 supervisor        exit=3
앱 사본 직접 실행         exit=3
SIGKILL 직후(stale 전)    exit=3
stale 회수 후             exit=0
--dry-run                 exit=0    계획은 claim 하지 않으므로 락 밖이다
```

supervisor 는 stale 창보다 오래 돌기 때문에 스캔마다 heartbeat 로 락을 갱신한다. 없으면
자기 락이 stale 로 판정돼 뺏긴다.

## 제거

```bash
.venv/bin/python scripts/kit_distribute.py --uninstall --app <id>
# 또는 직접
python3 tools/amplai-loop-kit/install.py --target <path> --uninstall
```

복제본에서 실행해 확인한 결과다. **17건이 설치되고 제거 후 2건이 남는다.**

2.3.0 은 **비워진 디렉토리도 정리한다.** 파일만 지우면 `.ai-team/install`·`.ai-team/local`
같은 빈 디렉토리가 남는데, `git` 이 빈 디렉토리를 추적하지 않아 `git status` 로는 안 보인다.
cortex 에서 그것 때문에 규약 test 가 제거 후에도 실패했고, 그 발견으로 고쳤다. 위로 올라가며
지우되 **비어 있지 않은 디렉토리에서 멈추므로** 앱이 쓰는 경로는 살아남는다.

| 남는 것 | 성격 | 처리 |
|---|---|---|
| `.ai-team/runtime/policy.json` | **내용은 원본과 정확히 같고 표기만 다르다** (실측 확인) | 아래 참조 |
| `.ai-team/backups/` | 설치·제거 시점의 백업 | **의도적이다.** 복구용이므로 남긴다 |

`AUTONOMY_POLICY.md` 와 `.claude/settings.json` 은 2.3.0 부터 지워진다. 설치가 install
record 에 `created_paths` 를 남겨 **"앱이 원래 갖고 있었다" 와 "우리가 만들었다" 를
구분**하기 때문이다. 그 전에는 둘 다 남았다.

### `policy.json` 표기 차이 — 알려진 한계

설치는 rule 을 텍스트로 덧붙여 나머지 줄을 byte-identical 로 둔다(2.2.0). **제거에는 그
대칭이 아직 없어서** 표준 포맷으로 다시 쓴다. 내용은 설치 전과 같지만 줄 배치가 달라져
`git diff` 에 잡힌다. 제거 출력의 note 가 그 사실을 알린다.

고치려면 `append_to_json_array` 의 역함수가 필요하다. **후속 과제로 남긴다.**

Store 는 제거 대상이 아니다 — `install.py --uninstall` 이 건드리지 않는다. 더 안 쓰면
디렉토리째 지운다.

## synapse 사본 삭제 — 참조 전수

`D-053` 이 synapse 의 `tools/amplai-loop-kit/` 를 지우기로 정했다. 그 저장소에서 kit 을
참조하는 자리를 두 방법으로 셌다 (`origin/main` = `1c05001b`).

```text
방법 1  경로 문자열 "tools/amplai-loop-kit"   4건 / 3파일
방법 2  이름 "amplai-loop-kit"                   5파일
```

두 값이 다르다 — 방법 2가 경로 없이 이름만 쓰는 자리를 더 잡는다.

| 참조 | 삭제 후 |
|---|---|
| `tests/ai/test_amplai_kit_installer.py` | **skip 된다.** docstring 이 "skipped when the kit source is not vendored" 로 그렇게 설계했다고 명시한다 |
| `.ai-team/runtime/policy.json:156` | path_rule pattern `tools/amplai-loop-kit/**` 이 매칭 대상을 잃는다. **깨지지 않지만 죽은 rule 이 된다** — 함께 지우는 편이 낫다 |
| `.ai-team/state/HANDOFF_2026-08-28_...md:5` | 역사 기록이라 그대로 유효하다 |
| `.ai-team/runtime/INSTALLATION.md:9,61` | kit 이름과 install record 경로 언급. 설치 산출물 쪽이라 유효하다 |
| `.gitignore:22` | `.ai-team/backups/amplai-loop-kit/` — 설치 산출물 경로라 유효하다 |

**삭제로 깨지는 것은 없다.** 정리할 것은 죽은 path_rule 하나다.

삭제 PR 은 그 저장소의 일이므로 여기서 열지 않는다. 위 표가 그 PR 의 근거다.

## 재설치가 되돌리는 것

`install.py` 는 idempotent update 라 재설치 때 marker 와 `.claude/settings.json` 을 다시
쓴다. 다음은 kit 소유가 아니므로 살아남는다.

```text
pyproject.toml 의 벤더링 lint 예외
.ai-team/README.md 의 규약 예외 각주 (D-051)
```

marker **안쪽**은 덮인다. 그 안을 고쳐야 하면 kit 의 `fragments/` 를 고치고 배포한다.
