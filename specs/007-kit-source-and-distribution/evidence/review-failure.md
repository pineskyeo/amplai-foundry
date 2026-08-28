# Review — Failure Lens (ALR-006)

- 대상 feature: `specs/007-kit-source-and-distribution/` (ALR-006)
- branch / HEAD: `mgc-012-package-3-wave-4` / `c2bd4e2`
- lens 범위: 실패 경로 — 락, 경합, 배포 안전장치, 제거 대칭성, fail-closed, 부분 실패 상태
- python: `/Users/pinesky/workspace/amplai-foundry/.venv/bin/python` (3.11.15), 모든 실행에 `PYTHONDONTWRITEBYTECODE=1`
- 격리 환경: `/Users/pinesky/.claude/jobs/65025e17/tmp/f1/`. `~/workspace/synapse`, `~/workspace/cortex`,
  `~/workspace/amplai-project` 에는 쓰지 않았다. 이 저장소에도 mutation 을 넣지 않았다
  (`git status --porcelain` 무출력, `git diff --stat` 무출력).

## Verdict

**FAIL.** blocker 여섯이다 — `P1` 하나, `Blocking-P2` 다섯.

방어선별 판정은 이렇다.

| 방어선 | 판정 | 근거 |
| --- | --- | --- |
| 단일 인스턴스 락 — 거부·직접 실행·stale 회수·dry-run 예외 | PASS | 4/4 재현 |
| 단일 인스턴스 락 — 경합에서 둘 다 사는가 | **FAIL** | F-4, F-5, F-6 |
| 배포 (a) 경로를 못 정하면 멈춘다 | PASS | 4 변형 전부 `EXIT_UNRESOLVED=3`, 대상 무변경 |
| 배포 (b) 전체 dry-run 통과 전 무설치 | PASS (우회 하나는 A-F1) | 주입 재현 |
| 배포 (c) 실패 지점에서 멈추고 보고 | PASS | 주입 재현, 부분 상태 복구됨 |
| 제거 대칭성 | **FAIL** | F-3 (앱이 쓴 내용까지 통째로 지운다) |
| Store 진입점 fail-closed | **FAIL** | F-2 (대조를 건너뛰는 입력 다섯) |
| 부분 실패 상태 / `--verify` 보고 | PASS | rollback 완전, `--verify` 정확 |
| seal.py 와 무결성 | **FAIL** | F-1 (봉인이 깨져 있고 읽는 코드가 없다) |

## Checked Items

### 1. 단일 인스턴스 락

sandbox: `tmp/f1/app1` (앱), `tmp/f1/store1` (Store). kit 2.3.0 정상 설치.

| 확인 | 재현 명령 | 결과 |
| --- | --- | --- |
| 첫 supervisor 가 락을 잡는다 | `run --run` 백그라운드 후 `ls store1/.amplai/locks/` | `supervisor.lock/owner.json` 에 pid 68160, token, `heartbeat_at` |
| 두 번째를 거부한다 | `store1/supervisor/run --run` | `another supervisor already holds this Project Store`, rc=3 (`EXIT_ALREADY_RUNNING`) |
| 앱 사본 직접 실행도 거부한다 | `app1/scripts/amplai_supervisor.py --project-home store1 --run` | 같은 메시지, rc=3 |
| dry-run 은 락 밖이다 | `... --dry-run` (락 점유 중) | `ok: true`, rc=0 |
| SIGKILL 직후에는 회수하지 않는다 | `kill -9` 후 `--once` | rc=3. stale 창 300 s 안이라 거부 (의도된 동작) |
| stale 창을 넘기면 회수한다 | `owner.json.heartbeat_at` 를 2020 으로 바꾸고 `--once` | rc=0, 정상 종료 후 lock dir 제거됨 |
| owner.json 없는 반쯤 만든 락은 훔치지 않는다 | `mkdir supervisor.lock` 후 `--once` | rc=3 (dir mtime fallback). mtime 을 1000 s 전으로 바꾸면 rc=0 |
| `heartbeat` 가 남의 락을 되살리지 않는다 | `tmp/f1/toctou.py` | 락을 뺏긴 holder 의 `heartbeat()` → `False`, owner.json 미변경 |
| `release` 가 남의 락을 지우지 않는다 | 같은 스크립트 | `LockError: project lock ownership changed`, lock dir 존속 |
| **경합에서 둘 다 사는가** | 아래 세 실험 | **산다** — F-4, F-5, F-6 |

경합 실험 셋이다.

```bash
# (i) 결정적 interleaving — 출하 파라미터 (stale 300 s) 그대로
.venv/bin/python /Users/pinesky/.claude/jobs/65025e17/tmp/f1/toctou.py
#   A._stale()=True, B._stale()=True  (둘 다 버려진 락을 보고 판단)
#   A._reclaim_stale()=True → A 가 새 락을 만들고 owner 를 쓴다
#   B._reclaim_stale()=True  ← A 의 갓 만든(=stale 아닌) 락을 rename 해 버린다
#   A.acquired=true, B.acquired=true, owner_token_is_B=true

# (ii) 실사용 파라미터 확률 측정 (버려진 stale 락 + 동시 기동)
.venv/bin/python .../race2.py 8 200 0     # {"trials_with_two_or_more_holders":0,"trials_with_unhandled_OSError":17,"total_acquires":200}
.venv/bin/python .../race2.py 8 200 1.0   # {"trials_with_two_or_more_holders":0,"trials_with_unhandled_OSError":29,"total_acquires":200}
.venv/bin/python .../race2.py 2 400 1.0   # {"trials_with_two_or_more_holders":0,"trials_with_unhandled_OSError":0,"total_acquires":400}

# (iii) stale 창을 좁힌 stress (stale 0.05 s, 24 thread, 60 round)
.venv/bin/python .../race.py
#   {"acquired":939,"overlapping_acquires":634,"max_concurrent_holders":4,"token_mismatch_while_holding":711}
```

수치는 두 방법으로 센다. 상호배제 위반은 (i) 결정적 interleaving 으로 **가능함이 증명**되고,
(iii) 에서 939 회 획득 중 634 회 중첩·최대 동시 보유 4 로 **관측**된다. 실사용 파라미터인 (ii)
에서는 200 회 시도에서 이중 보유 0 회다 — 창이 밀리초라 확률이 낮다는 뜻이지, 닫혀 있다는
뜻이 아니다. 반면 미처리 `OSError` 는 (ii) 에서 8 thread 200 trial 중 17·29 회로 관측되고,
2 thread 400 trial 에서는 0 회다.

이중 보유는 TOCTOU 없이도 난다. host suspend 로 heartbeat 가 300 s 이상 끊긴 경우다.

```bash
# supervisor #1 을 --run 으로 띄우고, owner.json 의 heartbeat_at 을 2020 으로 늙힌 뒤 #2 기동
lock now owned by pid: 69114
supervisor#1 pid=69106 alive=YES     # 락을 잃었는데 계속 돈다. stderr 는 비어 있다
supervisor#2 pid=69114 alive=YES
```

### 2. 배포 래퍼의 방어선 셋

`scripts/kit_distribute.py` 를 실제 모듈로 import 하고 `REPO_ROOT`·`TARGETS_FILE` 만 lab 으로
돌린 뒤 `main()` 을 호출했다 (`tmp/f1/lab/drive.py`). `INSTALLER`·`SELFTEST`·`KIT_ROOT` 는 실제
패키지를 가리킨다. 저장소 파일은 건드리지 않았다.

**(a) 경로를 못 정하면 멈춘다 — PASS (4/4).**

| 주입 | 결과 |
| --- | --- |
| `paths` 에서 t2 누락 | rc=3, `no host-local path configured`, hint 는 후보로만 보고. t1 파일 4 개 그대로 |
| t2 에 상대경로 `../t2` | rc=3, `configured path is not absolute` |
| t2 에 없는 경로 | rc=3, `configured path does not exist` |
| path_map 파일 자체 부재 | rc=3, 두 대상 모두 unresolved |

**(b) 전체 dry-run 통과 전 무설치 — PASS.**
t2 의 `.ai-team/runtime/policy.json` 을 지워 plan 불가로 만들고 `--all` 을 돌렸다. dirty gate 가
먼저 걸렸고(`targets have uncommitted changes`, rc=2), commit 해서 clean 으로 만든 뒤에도
t1 은 무변경(4 파일)이었고 Store 는 생성되지 않았다.

**(c) 실패 지점에서 멈추고 상태를 보고 — PASS.**
t2 의 `.ai-team/runtime/schemas` 를 `chmod 500` 으로 만들어 plan 은 통과하고 execute 만 실패하게 했다.

```json
{"action":"install","completed":[{"app_id":"t1","ok":true},
 {"app_id":"t2","ok":false,"stderr":"INSTALL_ERROR: [Errno 13] Permission denied: ...schemas/.amplai-install-2rloh2k6"}],
 "ok":false,"stopped_at":"t2"}
```

t1 은 설치 완료, t2 는 4 파일로 완전 rollback(잔여 없음), Store 에는 t1 만 등록됐다.
이어 `--verify` 가 `mismatched: ["t2"]`, `t2.installed: null` 로 정확히 보고했다.

**우회 경로.** symlink 둘 다 막힌다. `.ai-team` 을 target 밖 디렉토리로 symlink 하면
`manifest path escapes target: .ai-team/runtime/DECISION_ASYNC_PROTOCOL.md` (rc=2), owned file
하나를 `/tmp/evil-target.py` 로 symlink 하면 `refusing to replace symlink: scripts/amplai.py`
(rc=2) 이고 `/tmp/evil-target.py` 는 생성되지 않는다. `--app` + `--all` 조합은 A-F1 이다.

### 3. 제거 대칭성

`tmp/f1/u1` — git 커밋된 앱에 kit 을 설치하고 `--uninstall` 했다.

| 확인 | 결과 |
| --- | --- |
| owned file 26 개 제거 | PASS. removals 26 |
| marker section strip | PASS. `.agents/skills/*/SKILL.md`, `.ai-team/runtime/WORKFLOW.md` 에 marker 0 개 잔존 |
| `policy.json` 의미 복원 | PASS. 설치 전 값에서 kit 소유 rule 1 개·금지항목만 뺀 값과 **완전 일치** (`exp == after` → True). rule 6 → 5 |
| 빈 디렉토리 정리 | PASS. `.ai-team/install`, `.ai-team/local`, `.ai-team/runtime/schemas`, `.claude` 제거 |
| **앱이 쓰는 경로를 지키는가** | PASS. `scripts/app_tool.py`, `tests/ai/test_app.py` 를 둔 상태에서 `scripts/`·`tests/ai/` 는 남았다. `_prune_empty_dirs` 는 `os.listdir` 비어 있을 때만 지운다 |
| **`created_paths` 판단** | **FAIL — F-3** |
| 남는 것 | `.ai-team/backups/amplai-loop-kit/<ts>-<pid>-uninstall/` 45 항목 (A-F2) |

### 4. Store 진입점의 fail-closed

`tools/amplai-loop-kit/payload/store/supervisor/run` 을 실제 Store 로 실행했다.

| 입력 | 기대 | 결과 |
| --- | --- | --- |
| Store `VERSION=9.9.9`, 앱 `2.3.0` | 거부 | PASS. `kit version mismatch...`, rc=4 |
| Store `VERSION=1.0.0`, 앱 `2.3.0` | 거부 | PASS. rc=4 |
| `supervisor/source.json` 없음 | 거부 | PASS (코드 경로 `run:96`). 미실행 — Not Checked |
| source 앱 미등록 | 거부 | PASS (`run:100`). 미실행 — Not Checked |
| supervisor 스크립트 부재 | 거부 | PASS (`run:107`). 미실행 — Not Checked |
| **`VERSION` 파일 없음** | 거부 | **FAIL. rc=0, supervisor 를 그대로 실행** |
| **`VERSION` 빈 파일** | 거부 | **FAIL. rc=0** |
| **`VERSION` 공백만** | 거부 | **FAIL. rc=0** |
| **앱 install record 없음** (Store 는 9.9.9) | 거부 | **FAIL. rc=0** |
| **install record 에 `package_version` 키 없음** | 거부 | **FAIL. rc=0** |

대조를 건너뛰는 입력은 두 방법으로 센다. 코드에서 세면 `run:116` 의 guard 피연산자 둘
(`store_version`, `app_version`) 이 각각 falsy 가 되는 경로 = 2 종. 실행으로 세면 위 표의 구체
입력 5 개다. 통제군(진짜 불일치)은 rc=4 로 여전히 거부한다.

### 5. 부분 실패 상태

- 배포 중단 시 남는 상태: 위 (c) 참조. 실패한 대상은 `rollback_applied` 로 완전 복구되고,
  이미 끝난 대상은 `completed` 에, 중단 지점은 `stopped_at` 에 남는다. 복구 가능하다.
- `install.py` 단독 실패도 같다. `execute()` 는 `backup_and_apply()` 후 `configure_project_store()`
  실패 시 `rollback_applied` + `rollback_project_store` 를 부른다. 이 run 이 처음 만든 Store 만 지운다.
- `--verify` 가 그 상태를 정확히 보고한다 (t2 `installed: null`, `matches: false`, rc=2).
- 다만 dry-run 은 `configure_project_store` 를 전혀 타지 않는다. 즉 (b) 의 보증은 "앱 파일 쓰기"
  까지이고 Store 단계 실패는 dry-run 이 못 잡는다. 이건 wrapper docstring 이 "pretending the run
  was atomic" 을 하지 않겠다고 명시한 범위 안이라 blocker 로 올리지 않는다.

### 6. seal.py 와 무결성

```bash
cd tools/amplai-loop-kit && shasum -a 256 -c CHECKSUMS.sha256
#   distribution/targets.json: FAILED
#   shasum: WARNING: 1 computed checksum did NOT match
```

- 두 방법으로 센다. `shasum -c` 는 42 행 중 41 행 OK / 1 행 FAILED. 직접 대조하면 기록값
  `e51db961…` vs 실제 `198bfb52…`. 파일 목록 자체는 완전하다 (listed 42 = actual 42, 양방향 차집합 공집합).
- `grep -rn CHECKSUMS` 를 저장소 전체에 돌리면 코드 hit 은 `seal.py` 다섯 줄뿐이다.
  `install.py`, `selftest.py`, `kit_distribute.py` 어디도 읽지 않는다.
- 그래서 봉인이 깨진 상태에서 `selftest.py` 는 `rc=0`, check 10 개 통과이고 `kit_distribute.py --dry-run`
  의 preflight 는 `"selftest": {"ok": true}` 를 낸다. → F-1.
- 반면 manifest 쪽 대조는 산다. `install.py:328 validate_package` 가 owned_files 20 개와 fragment
  hash 를 전부 검사한다. payload 만 바꾸면 설치가 막힌다.

## New Blockers

| id | 등급 | 위치 | 내용 |
| --- | --- | --- | --- |
| F-1 | P1 | `tools/amplai-loop-kit/CHECKSUMS.sha256:9` | 봉인이 깨진 채다. `distribution/targets.json` 기록값 `e51db961…` ≠ 실제 `198bfb52…` (42 행 중 1 행). 게다가 이 파일을 **읽는 코드가 없다** — `grep -rn CHECKSUMS` 코드 hit 은 `seal.py` 뿐이다. 그래서 `selftest.py` rc=0, `kit_distribute --dry-run` preflight `selftest.ok=true` 로 깨진 패키지가 배포 경로를 통과한다. AC-002 ("CHECKSUMS.sha256 이 실제 파일 hash 와 일치한다") 불통과. c2bd4e2 가 `targets.json` 과 `CHECKSUMS.sha256` 을 같은 commit 에서 만졌으므로 reseal 뒤 targets.json 을 다시 고친 것이다 |
| F-2 | Blocking-P2 | `tools/amplai-loop-kit/payload/store/supervisor/run:116` | `if store_version and app_version and store_version != app_version` 이라 한쪽이 없으면 대조를 **건너뛰고 실행한다**. 주석은 "Refuse instead of guessing" 이라고 쓰여 있는데 모르면 실행하는 쪽으로 열려 있다. 재현 5/5: `VERSION` 부재 rc=0, `VERSION` 빈 파일 rc=0, `VERSION` 공백만 rc=0, install record 부재 rc=0, `package_version` 키 부재 rc=0. 통제군(9.9.9 vs 2.3.0) 은 rc=4 |
| F-3 | Blocking-P2 | `tools/amplai-loop-kit/install.py:679` | `created_paths` 에 든 marker 파일은 strip 후 남은 내용을 **확인하지 않고 파일 전체를 지운다**. 앱이 그 파일에 자기 내용을 덧붙였으면 함께 사라진다. 재현: 설치 후 `.gitignore` 에 3 줄, `.ai-team/AUTONOMY_POLICY.md` 에 2 줄을 앱이 추가 → `--uninstall` → 두 파일 모두 `DELETED`, `node_modules` 와 `keep me` 소실. `.ai-team/backups/.../uninstall/` 에 백업은 있으나 보고서 `notes` 에 경고가 없다. 형제 위치를 끝까지 세면 같은 판단이 세 군데인데 `install.py:725` 의 `.claude/settings.json` 만 `not merged` 로 빈 것을 확인하고, `install.py:679` 는 확인하지 않는다. 위험 대상은 `create_if_missing` marker 2 개 (`.ai-team/AUTONOMY_POLICY.md`, `.gitignore`) — manifest 로 세도 2, 실제 install record 의 `created_paths` 3 개 중 settings.json 을 뺀 2 로 세도 2 |
| F-4 | Blocking-P2 | `tools/amplai-loop-kit/payload/scripts/amplai_supervisor.py:314` | `self.heartbeat()` 의 반환값을 버린다. 락을 stale 로 뺏긴 supervisor 는 `heartbeat()` 가 `False` 를 돌려줘도 그대로 돈다. 재현: `--run` 중인 pid 69106 의 `owner.json.heartbeat_at` 을 2020 으로 늙히고 두 번째 supervisor 기동 → 락 소유 pid 69114, **69106 과 69114 가 둘 다 살아 있고** 69106 의 stderr 는 비어 있다. host suspend 가 300 s 를 넘기면 재현된다. `store.lock()` 이 `claim_work` 를 지키므로 같은 Work 를 두 번 claim 하지는 않지만, "Only one supervisor may run against a Store" 라는 `amplai_supervisor.py:383` 의 불변식은 깨진다 |
| F-5 | Blocking-P2 | `tools/amplai-loop-kit/payload/scripts/amplai_runtime.py:408-416` | reclaim 경합에서 `acquire()` 가 `LockError` 가 아니라 날 `OSError` 를 던진다. `os.mkdir` 성공 뒤 다른 contender 가 그 디렉토리를 rename 해 가면 `write_json_atomic` 이 `FileExistsError`(`amplai_runtime.py:225`) 또는 `FileNotFoundError`(`:248`, `:249`, `:240`) 로 터진다. 측정: stale 락 + 8 thread 동시 기동 200 trial 중 17 회(wait 0) / 29 회(wait 1.0), 2 thread 400 trial 중 0 회. 결과가 fail-closed 이긴 하나 supervisor 는 `EXIT_ALREADY_RUNNING=3` 이 아니라 `EXIT_ERROR=2` 로 죽고, `store.lock()` 을 쓰는 모든 Store 쓰기 경로가 10 s 재시도 대신 즉시 예외로 끝난다 |
| F-6 | Blocking-P2 | `tools/amplai-loop-kit/payload/scripts/amplai_runtime.py:392-397` | `_stale()` 판정과 `os.rename` 사이가 원자적이지 않아 **갓 만들어진(=stale 아닌) 락을 뺏는다**. `AtomicDirectoryLock` docstring 은 "a stale lock is reclaimed by moving it aside with a single atomic rename, so exactly one contender can win the reclaim race" 라고 주장하는데, rename 은 원자적이어도 "무엇을 rename 하는가" 는 판정 시점과 다르다. 결정적 재현(`toctou.py`, 출하 stale 300 s): A·B 가 같은 버려진 락을 보고 stale 판정 → A 가 회수하고 새 락 확보 → B 가 A 의 새 락을 rename → `A.acquired=true` 이면서 `B.acquired=true`. 확률 관측: stale 0.05 s / 24 thread 에서 939 획득 중 634 중첩, 최대 동시 보유 4. 이 락은 supervisor 락뿐 아니라 sealed event chain 을 포함한 모든 Store 변경을 지키는 `store.lock()` 이기도 하다 |

## Verified Clean

- 두 번째 supervisor 거부, 앱 사본 직접 실행 거부 — 둘 다 `EXIT_ALREADY_RUNNING=3`. 락이 Store 에
  있어 어느 사본을 띄우든 같은 디렉토리를 다툰다 (`amplai_runtime.py:571 supervisor_lock`).
- `--dry-run` 은 락 밖이다. 점유 중에도 rc=0 (`amplai_supervisor.py:369` 의 early return).
- stale 회수가 창을 넘겨서만 일어난다. SIGKILL 직후 rc=3, `heartbeat_at` 을 늙히면 rc=0.
- owner.json 없는 반쯤 만든 락은 dir mtime fallback 으로 보호된다 (`amplai_runtime.py:383-389`).
- `heartbeat()` 가 남의 락을 되살리지 않는다 — token 불일치면 `False` 반환, 쓰지 않는다.
- `release()` 가 남의 락을 지우지 않는다 — token 불일치면 `LockError`, `shutil.rmtree` 에 도달하지 않는다.
- `describe_owner()` 는 owner.json 이 없어도 예외를 내지 않고 `None` 을 준다.
- 배포 (a): 경로 미해결 4 변형 전부 `EXIT_UNRESOLVED=3`, 대상 무변경. hint 는 `candidate_from_hint`
  로만 보고하고 그 경로에 쓰지 않는다.
- 배포 (b): plan 실패 시 아무 대상에도 설치하지 않는다.
- 배포 (c): 실패 대상 완전 rollback, `stopped_at`/`not_attempted` 보고, `--verify` 가 그 상태를 정확히 반영.
- dirty gate: 대상에 uncommitted 변경이 있으면 install 을 막고 dry-run 에서는 보고만 한다.
- symlink 우회 둘 다 차단. `safe_destination` 이 상위 이탈(`manifest path escapes target`)과
  symlink 덮어쓰기(`refusing to replace symlink`) 를 모두 거부한다.
- `_prune_empty_dirs` 가 앱이 쓰는 경로를 지우지 않는다. `os.listdir` 이 비어 있을 때만 `rmdir`,
  target root 에서 멈춘다.
- uninstall 의 `policy.json` 복원이 의미적으로 정확하다 (설치 전 값 − kit 소유 rule = 복원값, 완전 일치).
- marker section strip 후 관리 마커 잔존 0.
- `install.py:328 validate_package` 가 owned_files 20 + fragment 전부의 manifest hash 를 대조한다.
- `--force` 없이 pre-existing 동명 파일이 있으면 `preflight conflicts` 로 막는다 (rc=2).
- 앱 install record 의 `package_version` 을 `installed_version()` 이 올바른 필드명으로 읽는다
  (`kit_distribute.py:139`, `run:82`). 실제 `--verify` 가 amplai-foundry/synapse 를 `2.3.0` 일치로 본다.

## Advisory

| id | 위치 | 내용 |
| --- | --- | --- |
| A-F1 | `scripts/kit_distribute.py:236` | `--all` 과 `--app` 이 mutually exclusive group 밖에서 조합된다. `--all --app t1` 은 경고 없이 t1 만 설치한다. 재현: t2 가 plan 불가인 상태에서 `--all --app t1` → `EXIT=0`, t1 파일 4 → 30. dry-run gate 는 "선택된 대상 전부" 로 좁혀지는데 `--all` 의 help 는 "Install into every target" 이다. 의도된 범위 제한이라면 help 문구를 맞추고, 아니라면 조합을 거부해야 한다 |
| A-F2 | `tools/amplai-loop-kit/install.py:752` | `--uninstall` 이 자기 백업을 `.ai-team/backups/amplai-loop-kit/<ts>-<pid>-uninstall/` 에 만들고 지우지 않는다. 관측 45 항목. 그래서 uninstall 후에도 `.ai-team` 최상위 항목이 하나 늘어난 상태로 남는다 — `distribution/targets.json` 의 cortex note 가 적은 `test_ai_team_has_no_new_top_level_directory` 가 걸리는 바로 그 종류다. 보고서 `notes` 에 백업 위치 언급이 없다 (`backup_dir` 필드에만 있다) |
| A-F3 | `tools/amplai-loop-kit/install.py:660-666` | `--force` 로 앱의 기존 동명 파일을 덮어쓴 뒤 `--uninstall` 하면 그 파일이 **삭제된다**. 원래 내용은 설치 시 백업(`.ai-team/backups/.../scripts/amplai.py`)에만 남고 복원되지 않는다. 재현: 앱 고유 `scripts/amplai.py` → `--force` 설치 → `--uninstall` → 파일·`scripts/` 디렉토리 모두 소멸. `--force` 가 명시적이고 백업이 있으니 advisory 로 둔다 |
| A-F4 | `tools/amplai-loop-kit/payload/scripts/amplai_runtime.py:372-389` | `_stale()` 이 owner.json 의 `pid`/`host` 를 기록만 하고 쓰지 않는다. 같은 host 에서 supervisor 가 죽으면 pid 가 증명 가능하게 없는데도 자기 Store 를 최대 300 s 막는다 (재현: SIGKILL 직후 rc=3). host 가 같을 때 `os.kill(pid, 0)` 으로 즉시 회수할 수 있다 |
| A-F5 | `scripts/kit_distribute.py:214` | `--verify` 가 "의도적으로 배포하지 않음" 을 표현하지 못한다. cortex 는 `targets.json` 의 note 로 제외가 결정돼 있는데 `installed: null` → `mismatched` → rc=2 다. 즉 정상 상태에서 `--verify` 는 항상 실패한다 (실제 실행에서 확인). target 에 `expect_installed` 같은 표식이 필요하다 |

## Not Checked

- `run` 의 나머지 fail-closed 분기 셋 — `source.json` 부재(`run:96`), source 앱 미등록(`run:100`),
  supervisor 스크립트 부재(`run:107`). 코드는 읽었고 전부 refuse 로 보이나 **실행으로 확인하지 않았다**.
- 실제 대상 저장소(`~/workspace/synapse`, `~/workspace/cortex`)와 Store(`~/workspace/amplai-project`)
  에 대한 쓰기 동작. 지시대로 읽기만 했다. `--verify` 만 실행했다.
- macOS APFS 밖의 파일시스템에서의 `os.mkdir`/`os.rename` 원자성. NFS·SMB 는 확인 못 함.
  락 결론은 로컬 APFS 기준이다.
- `tools/amplai-loop-kit/` 이관 원본 자체 (지시상 범위 밖). `amplai_runtime.py` 는
  `AtomicDirectoryLock`, `ProjectStore.lock/supervisor_lock`, `claim_work` 만 읽었다.
- kit 이 설치하는 `tests/ai/` 3 개 test 파일의 내용과 통과 여부.
- `seal.py` 를 실행해 봉인을 다시 만들었을 때의 결과. mutation 금지 원칙에 따라 실행하지 않았다.
- `--allow-dirty` 로 실제 fleet install 을 강행했을 때의 동작.
- 여러 `--project-home` 을 섞은 배포 (wrapper 는 전 대상에 하나만 넘긴다).
