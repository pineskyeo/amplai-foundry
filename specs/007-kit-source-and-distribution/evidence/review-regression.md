# Review — Regression Lens (ALR-006)

- 대상 — `specs/007-kit-source-and-distribution/`, branch `mgc-012-package-3-wave-4`, HEAD `af22ab8`
- 범위 — test 가 무엇을 고정하는지. 적어 둔 test 가 대상을 실제로 치는지, mutation 으로 죽는지,
  주장과 검증이 어긋나지 않는지.
- 방법 — 유효 mutation 31 건을 코드에 넣고 해당 test 를 돌린 뒤 되돌렸다. 모든 실행은
  `PYTHONDONTWRITEBYTECODE=1` 과 `__pycache__` 삭제를 앞에 두었다. python 은 `.venv/bin/python` 이다.

## Verdict

**Blocking.** blocker 7 건 — `P1` 4, `Blocking-P2` 3.

기능 코드는 이번 라운드에서 검사한 범위 안에서 옳다. 결함은 **test 쪽**이다. mutation 31 건 중
10 건이 살아남았고, 그중 9 건이 진짜 검증 공백이다. 살아남은 자리 넷은 test 이름이 그 방어선을
명시적으로 주장하는데도 실제로는 다른 경로로 통과하거나 그 상태를 만들지 못한다. 앞선 두 lens 가
낸 결함 여럿이 test 가 못 잡아서 살아남은 것이었다는 이력과 같은 형태가 그대로 남아 있다.

배포 래퍼와 installer 는 **다른 repository 에 쓴다.** 그 두 곳의 미고정 방어선이 이번 등급의 근거다.

## Mutation Table

유효 mutation 31 건. `KILLED` 20, `KILLED_BY_HANG` 1, `SURVIVED` 10.

| id | 대상 | mutation | 결과 | 죽인 test |
|---|---|---|---|---|
| M1 | `amplai_runtime._reclaim_stale` | `if moved != expected_token:` → `if False:` | KILLED | `SupervisorLockRaceTest::test_reclaim_does_not_steal_a_lock_created_after_the_judgement` |
| M2 | `amplai_runtime._reclaim_stale` | `expected_token is not None` → `is None` | KILLED | 위와 같음 |
| M3 | `amplai_runtime.heartbeat` | token 대조 제거 | KILLED | `SupervisorSingleInstanceTest::test_heartbeat_refuses_after_the_lock_was_reclaimed` |
| M4 | `amplai_runtime.release` | 소유권 대조 제거 | KILLED | `LockTest::test_release_after_stale_reclaim_keeps_new_owner` |
| M5 | `amplai_runtime.ensure_dir` | `except OSError` 의 isdir 재확인 제거 | KILLED | `SupervisorLockRaceTest::test_concurrent_acquire_yields_exactly_one_holder` |
| **M6** | `amplai_runtime._stale_owner` | `heartbeat_at or created_at` → `created_at` | **SURVIVED** | — |
| M7 | `amplai_runtime.acquire` | `if stale and ...` → `if ...` | KILLED | `test_release_after_stale_reclaim_keeps_new_owner`, `test_second_holder_is_refused_without_waiting`, `test_concurrent_acquire_yields_exactly_one_holder`, `test_contention_raises_lock_error_not_a_bare_os_error` |
| **M8** | `amplai_runtime.acquire` | `except OSError as exc:` → `except ZeroDivisionError as exc:` | **SURVIVED** | — |
| M9 | `amplai_supervisor.run_until_quiescent` | `if self.heartbeat() is False:` → `if False:` | KILLED_BY_HANG | 종료하지 않는다 (A-R1) |
| M10 | `amplai_supervisor.run_until_quiescent` | `is False` → `not self.heartbeat()` | KILLED | `SupervisorSingleInstanceTest::test_supervisor_passes_its_heartbeat_into_the_scan_loop` |
| M11 | `kit_distribute.main` | `if unresolved:` → `if False:` | KILLED | `TestRunGates::test_an_unresolved_target_stops_the_run` |
| M12 | `kit_distribute.main` | `failed_plans` → `[]` | KILLED | `TestRunGates::test_a_failed_plan_installs_nothing` |
| **M13** | `kit_distribute.main` | 설치 loop 의 `break` 제거 | **SURVIVED** | — |
| M14 | `kit_distribute.cmd_verify` | `found == expected` → `found is None or ...` | KILLED | `TestVerify::test_a_target_with_no_install_record_counts_as_mismatched` |
| **M15** | `install.plan_uninstall` | `created = set(state["created_paths"])` → `set()` | **SURVIVED** | — |
| M16 | `install._prune_empty_dirs` | `if os.listdir(current): break` 제거 | SURVIVED (equivalent) | — (A-R2) |
| **M17** | `install._only_kit_header` | `return remainder == header.strip()` → `return True` | **SURVIVED** | — |
| M18 | `install` 버전 판정 | downgrade 거부 제거 | KILLED | `InstallerTest::test_downgrade_is_refused_without_force` |
| **M19** | `seal.verify` | `ok = not (...)` → `ok = True` | **SURVIVED** | — |
| **M20** | `seal.verify` | `manifest_stale.append(...)` → `pass` | **SURVIVED** | — |
| **M21** | `store/supervisor/run` | `if not store_version or not app_version:` → `if False:` | **SURVIVED** | — |
| **M22** | `store/supervisor/run` | `if store_version != app_version:` → `if False:` | **SURVIVED** | — |
| M23 | `kit_distribute.main` | selftest gate 제거 | KILLED | `TestRunGates::test_a_failed_selftest_stops_before_any_plan` |
| M24 | `kit_distribute.main` | dirty gate 제거 | KILLED | `TestRunGates::test_a_dirty_target_blocks_install_but_not_a_plan` |
| M26 | `kit_distribute.order_targets` | 순서 부여 제거 | KILLED | `TestOrdering` |
| M27 | `kit_distribute.resolve_paths` | `is_absolute` 검사 제거 | KILLED | `TestPathResolution::test_a_relative_configured_path_is_refused` |
| M28 | `kit_distribute.resolve_paths` | `is_dir` 검사 제거 | KILLED | `TestPathResolution::test_a_missing_configured_path_is_refused` |
| M29 | `kit_distribute.resolve_paths` | hint 를 경로로 채택 | KILLED | `test_a_target_without_a_configured_path_is_unresolved`, `test_the_hint_is_offered_as_a_candidate_never_used`, `test_an_unresolved_target_stops_the_run` |
| M30 | `ProjectStore.supervisor_lock` | lock 경로를 다른 이름으로 | KILLED | `SupervisorSingleInstanceTest::test_lock_lives_in_the_store_not_the_application` |
| M31 | `ProjectStore.supervisor_lock` | `wait_seconds` `0` → `30` | KILLED | `SupervisorSingleInstanceTest::test_second_holder_is_refused_without_waiting` |
| M32 | `ProjectStore.supervisor_lock` | `stale_seconds` 기본값 `300` → `0` | KILLED | `test_heartbeat_keeps_a_long_run_from_being_reclaimed`, `test_second_holder_is_refused_without_waiting` |

M25 는 적용 대상 문자열이 없어 폐기했고 번호를 재사용하지 않았다.

## Blockers

| id | 등급 | 한 줄 |
|---|---|---|
| R-1 | P1 | 설치 실패 시 중단(`break`)을 고정하는 test 가 없다 — fixture 가 target 둘뿐이라 실패가 항상 마지막이다 |
| R-2 | P1 | `_only_kit_header` 에 test 가 없다 — 무력화하면 앱이 쓴 내용이 삭제되는데 12 건이 모두 통과한다 |
| R-3 | P1 | `store/supervisor/run` 에 test 가 하나도 없다 — 버전 대조 두 gate 가 전혀 고정돼 있지 않다 |
| R-4 | P1 | `test_heartbeat_keeps_a_long_run_from_being_reclaimed` 가 이름이 주장하는 상태를 만들지 못한다 |
| R-5 | Blocking-P2 | `test_contention_raises_lock_error_not_a_bare_os_error` 가 timeout 분기로 통과한다 — 주장하는 wrap 은 미검증이다 |
| R-6 | Blocking-P2 | `created_paths` 판단을 고정하는 test 가 없다 — 무력화하면 uninstall 이 파일을 남긴다 |
| R-7 | Blocking-P2 | `seal.py --verify` 에 test 가 없다 — 검사 항목 전체가 미고정이다 |

### R-1 — 설치 실패 시 중단이 고정돼 있지 않다 (P1)

`tests/test_kit_distribute.py` 의 module docstring 은 세 규칙을 주장한다 — "never guess a path,
never install after a failed plan, **and stop at the first failure instead of continuing**".
앞의 둘은 M11·M12 가 죽는다. 셋째는 죽지 않는다.

`config` fixture(`tests/test_kit_distribute.py:23-32`)의 target 은 `alpha` 와 `beta` 둘뿐이다.
`test_an_install_failure_stops_and_reports_what_was_done`(`tests/test_kit_distribute.py:186`)은
**마지막** target 인 `beta` 를 실패시킨다. `break` 가 건너뛸 대상이 애초에 없으므로 loop 는
어차피 끝난다. 단언도 `completed == ["alpha", "beta"]` 와 `stopped_at == "beta"` 뿐이고
`not_attempted` 가 비어 있지 않은지는 어디서도 확인하지 않는다.

target 셋으로 같은 시나리오를 돌려 차이를 확인했다.

```text
UNMUTATED  real installs attempted: ['alpha', 'beta']
           stopped_at: beta | not_attempted: ['gamma']
MUTATED    real installs attempted: ['alpha', 'beta', 'gamma']
           stopped_at: beta | not_attempted: []
```

`.ai-team/kit-targets.json` 의 실제 app 은 셋이다. 래퍼가 다른 repository 에 쓰는 이상, 실패
이후 진행을 막는 규칙은 고정돼야 한다.

**요구** — target 셋 이상에서 중간 target 을 실패시키고 `not_attempted` 가 비어 있지 않음을
단언하는 test 를 추가한다.

### R-2 — `_only_kit_header` 가 미검증이고 무력화하면 앱 내용이 사라진다 (P1)

`tools/amplai-loop-kit/install.py:793` 의 `_only_kit_header` 는 uninstall 이 kit 이 만든
marker 파일을 지울지 내용을 남길지 가르는 유일한 판단이다. `return True` 로 바꿔도 installer
test 12 건이 모두 통과한다.

manifest 의 marker 7 개 중 `create_header` 를 가진 것은 `.ai-team/AUTONOMY_POLICY.md` 하나다.
`InstallerTest.setUp` 은 `make_target(with_autonomy=True)` 로 이 파일을 **미리 만들어 둔다**
(`tests/ai/test_amplai_kit_installer.py:43,69-71`). 따라서 이 파일은 `created_paths` 에 들어가지
않고, uninstall 의 created-marker 분기 한 쌍은 test suite 안에서 한 번도 실행되지 않는다.

kit 이 이 파일을 만들도록(사전 생성 없이) 설치한 뒤, 앱이 그 파일에 자기 규칙을 덧쓰고
uninstall 한 결과다.

```text
UNMUTATED  AUTONOMY_POLICY.md in created_paths: True
           file still exists: True    app rule preserved: True
M17        AUTONOMY_POLICY.md in created_paths: True
           file still exists: False   app rule preserved: FILE DELETED - app content lost
```

**요구** — `with_autonomy=False` 로 설치해 kit 이 파일을 만들게 한 뒤, 앱 내용을 덧쓰고
uninstall 해 파일과 앱 내용이 남는지 단언하는 test 를 추가한다.

### R-3 — `store/supervisor/run` 에 test 가 하나도 없다 (P1)

`tools/amplai-loop-kit/payload/store/supervisor/run` 을 참조하는 test 는 repository 전체에 없다.
M21·M22 로 두 gate 를 각각 무력화했고 in-scope 93 건이 모두 통과했다.

이 파일은 "kit 버전이 어긋나는 supervisor 사본을 실행하지 않는다" 는 fail-closed gate 다.
직접 실행해 두 gate 가 현재는 동작함을 확인했다.

```text
A  store=1.0.0-wrong, app=2.3.0 → rc=4, "kit version mismatch between this Store and the source application"
B  store="" (빈 값),  app=2.3.0 → "cannot compare kit versions between this Store and the source application"
```

방어선은 **있다**. 고정하는 test 가 없을 뿐이다. schema 와 lock semantics 가 어긋난 supervisor
가 Store 를 잡는 것이 이 gate 가 막는 사고이고, 그 사고는 되돌리기 어렵다.

**요구** — 임시 Store 를 만들어 (a) 버전 불일치가 `EXIT_VERSION_MISMATCH`(4) 로 거부되는지,
(b) 어느 한쪽 버전이 비면 거부되는지 단언하는 test 를 추가한다.

### R-4 — 장기 실행 보호 test 가 자기 이름의 상태를 만들지 못한다 (P1)

`_stale_owner` 의 `marker = owner.get("heartbeat_at") or owner.get("created_at")`
(`scripts/amplai_runtime.py:388`)를 `created_at` 만 읽도록 바꿔도 test 가 죽지 않는다.

`test_heartbeat_keeps_a_long_run_from_being_reclaimed`
(`tests/ai/test_amplai_kit_regressions.py:566`)는 `heartbeat_at` 과 `created_at` 을 **둘 다**
과거로 바꾼 뒤 `heartbeat()` 를 부른다. `heartbeat()` 는 `created_at` 에 `self.created_at` 을
다시 쓰는데, test 에서는 그 값이 방금 전이다. 그래서 heartbeat 이후 두 field 가 모두 최신이 되고
`_stale()` 은 어느 field 를 읽든 False 다. **이 test 는 두 field 를 구별할 수 없다.**

production 의 장기 실행은 `created_at` 이 실제로 오래고 `heartbeat_at` 만 최신이다. 그 상태를
직접 만들어 차이를 확인했다.

```text
owner record: {'created_at': '2000-01-01T00:00:00Z', 'heartbeat_at': '2026-08-28T07:13:16Z'}
UNMUTATED  _stale() on a live long run -> False
M6         _stale() on a live long run -> True   (살아 있는 supervisor 가 회수된다)
```

주석이 명시한 의도("Prefer the newer field when present so a supervisor that is still running is
not reclaimed underneath it", `scripts/amplai_runtime.py:385-387`)가 미검증이다. 무력화의 결과는
supervisor 둘이며, 이는 이번 lock 작업 전체가 막으려는 바로 그 상태다.

**요구** — `created_at` 만 과거로 만들고 `heartbeat_at` 은 최신으로 둔 owner record 에서
`_stale()` 이 False 임을 단언하는 test 를 추가한다.

### R-5 — LockError wrap test 가 다른 분기로 통과한다 (Blocking-P2)

`test_contention_raises_lock_error_not_a_bare_os_error`
(`tests/ai/test_amplai_kit_regressions.py:682`)는 이름과 docstring 으로 `acquire()` 안의
`except OSError → LockError` wrap 을 주장한다. 그 `except` 절을 `except ZeroDivisionError` 로
바꿔도 test 가 통과한다(M8).

이유는 test 가 그 분기를 지나지 않기 때문이다. blocker 가 `stale=300` 으로 살아 있으므로
`_stale_owner()` 는 `(False, token)` 을 돌려주고, 안쪽 `try` 는 OSError 를 내지 않는다.
LockError 는 그 아래 timeout 분기에서 나온다. 실제 message 를 찍어 확인했다.

```text
LockError message: timed out acquiring project lock: /var/folders/.../locks/sup.lock
-> came from the TIMEOUT branch, not the OSError-wrap branch
```

`scripts/amplai_runtime.py:455-461` 의 wrap 은 검증된 적이 없다.

**요구** — `_stale_owner` 가 OSError 를 내도록 만든 상태에서 `acquire()` 가 `LockError` 를
내는지 단언하는 test 를 추가한다. 현재 test 는 이름을 timeout 을 가리키도록 고치거나 둘로 나눈다.

### R-6 — `created_paths` 판단이 고정돼 있지 않다 (Blocking-P2)

`created = set(self.state.get("created_paths") or [])`(`tools/amplai-loop-kit/install.py:655`)
를 `set()` 으로 바꿔도 12 건이 통과한다. R-2 와 같은 원인 — created-marker 분기가 test 안에서
실행되지 않는다.

kit 이 만들고 앱이 손대지 않은 파일로 확인한 결과다.

```text
UNMUTATED  file still exists: False   (uninstall 이 자기가 만든 파일을 지운다)
M15        file still exists: True    (빈 껍데기가 남는다)
```

`test_uninstall_leaves_no_directory_it_created`(`tests/ai/test_amplai_kit_installer.py:213`)는
디렉토리만 보고 파일은 보지 않는다.

**요구** — R-2 의 test 에 "앱이 손대지 않았으면 파일까지 사라진다" 는 대칭 case 를 함께 넣는다.

### R-7 — `seal.py --verify` 에 test 가 없다 (Blocking-P2)

`seal.py --verify` 를 부르는 test 는 없다. `ok` 계산을 `ok = True` 로 고정해도(M19),
`manifest_stale` 수집을 없애도(M20) in-scope 93 건이 모두 통과한다.

이 명령은 verifier registry 의 `kit-seal`(`.ai-team/verifiers/registry.json:144-145`)로 매 실행
호출되지만, verifier 는 **통과 경로만** 지나간다. 즉 payload 훼손은 오늘 잡히지만 `verify()`
자신의 회귀는 아무도 잡지 못한다. gate 가 조용히 무력화될 수 있다.

방어선 자체는 동작한다. payload 파일 하나를 훼손해 확인했다.

```text
rc=1  ok: False
checksum_mismatched: ['payload/scripts/amplai_supervisor.py']
manifest_stale:      ['owned_files:scripts/amplai_supervisor.py']
```

**요구** — 임시 package 사본에서 (a) checksum 불일치, (b) CHECKSUMS 미등재 파일,
(c) manifest stale 각각에 대해 `verify()` 가 1 을 돌려주는지 단언하는 test 를 추가한다.

## Verified Clean

mutation 으로 죽었고, 죽인 test 가 그 방어선을 실제로 치는 것을 확인한 자리다.

- **락 — 두 번째 거부.** M31 이 `test_second_holder_is_refused_without_waiting` 로 죽는다.
  `wait_seconds=0` 이 대기 없는 거부를 만든다는 사실이 고정돼 있다.
- **락 — 앱 사본 거부.** M30 이 `test_lock_lives_in_the_store_not_the_application` 로 죽는다.
  lock 이 Store 의 `.amplai/locks/` 에 있다는 것이 경로 수준에서 고정돼 있다.
- **락 — stale 회수.** M7(`stale` 판정 무시)이 test 넷으로 죽는다. M32(`stale_seconds` 기본값)도
  죽는다.
- **락 — heartbeat 실패 시 정지.** M10(`is False` → truthiness)이
  `test_supervisor_passes_its_heartbeat_into_the_scan_loop` 로 죽는다. `is False` 라는 좁은 판정이
  의도적으로 고정돼 있다.
- **락 — reclaim 의 token 대조.** M1·M2 가 모두
  `test_reclaim_does_not_steal_a_lock_created_after_the_judgement` 로 죽는다. 이 test 는 실제
  파일과 실제 rename 을 쓴다.
- **락 — 소유권 이전 후 heartbeat/release 거부.** M3·M4 가 각각 전용 test 로 죽는다.
- **`ensure_dir` 의 except.** M5 가 `test_concurrent_acquire_yields_exactly_one_holder` 로 죽는다.
  이 test 는 실제 thread 경합을 쓴다.
- **배포 래퍼 — 경로 미해결 시 중단.** M11·M27·M28·M29 가 모두 죽는다. 경로 해결 규칙 넷
  (미설정·상대경로·부재·hint 미채택)이 각각 전용 test 로 고정돼 있다.
- **배포 래퍼 — 전체 dry-run 통과 전 미설치.** M12 가 `test_a_failed_plan_installs_nothing` 로
  죽는다.
- **배포 래퍼 — selftest·dirty gate.** M23·M24 가 각각 죽는다.
- **버전 대조 fail-closed (`cmd_verify`).** M14 가
  `test_a_target_with_no_install_record_counts_as_mismatched` 로 죽는다. install record 부재를
  일치로 취급하지 않는다.
- **downgrade 거부.** M18 이 `test_downgrade_is_refused_without_force` 로 죽는다.

### Mock 과 실제

앞선 라운드의 결함(install record field 를 `version` 으로 지어낸 mock)은 닫혔다.
`test_installed_version_reads_the_field_the_installer_writes`(`tests/test_kit_distribute.py:290`)가
installer 가 실제로 쓴 record 를 읽고 `package_version` 을 단언한다.

남은 mock 의 형식을 실제와 대조했고 어긋난 곳은 없다.

- `TestRunGates` 의 가짜 `run_installer` 는 실제 signature
  (`target, config, *, dry_run, uninstall=False, project_home=None`,
  `scripts/kit_distribute.py:153-160`)와 일치한다.
- 가짜 `preflight` 가 돌려주는 key 셋(`selftest`, `kit_version`, `strict_clean`)은 실제
  `preflight`(`scripts/kit_distribute.py:192-207`)와 일치하고, `selftest.detail` 의
  list-or-None 형태도 맞다.

실제를 쓰는 test 와 mock 만 쓰는 test 의 구분은 이렇다. lock test(`SupervisorSingleInstanceTest`,
`SupervisorLockRaceTest`, `LockTest`)는 실제 디렉토리·실제 rename·실제 thread 를 쓴다.
installer test 12 건은 실제 subprocess 로 `install.py` 를 돌린다. `TestRunGates` 6 건만 mock 이고,
`TestRealPackage` 4 건이 실제 config·실제 record 로 그 mock 을 받친다.

## Advisory

| id | 한 줄 |
|---|---|
| A-R1 | heartbeat 정지 test 가 방어선 제거 시 실패가 아니라 무한 hang 이 된다 |
| A-R2 | `_prune_empty_dirs` 의 `listdir` guard 는 `os.rmdir` 과 중복이라 test 가 구별할 수 없다 |
| A-R3 | 배포된 앱에서는 installer test 12 건이 전부 조용히 skip 된다 |

### A-R1

M9(`if self.heartbeat() is False:` → `if False:`)를 넣으면
`test_a_failed_heartbeat_stops_the_scan_loop`(`tests/ai/test_amplai_kit_regressions.py:741`)가
`persistent=True` 로 영원히 돈다. 90 초 timeout 으로 끊어야 했다. 방어선을 지우면 red test 가
아니라 hang 이 된다. `pytest-timeout` 은 이 환경에 설치돼 있지 않다. 검출은 되므로 blocker 는
아니지만, CI 에서 원인을 읽기 어려운 형태다.

### A-R2

`_prune_empty_dirs`(`tools/amplai-loop-kit/install.py:809-826`)의 `if os.listdir(current): break`
를 지워도 test 가 죽지 않는다. `os.rmdir` 자체가 비어 있지 않은 디렉토리에 OSError 를 내고
아래 `except OSError: break` 가 그것을 받기 때문이다. 즉 equivalent mutation 이고 코드 결함이
아니다. 다만 `test_uninstall_keeps_a_directory_the_app_still_uses`
(`tests/ai/test_amplai_kit_installer.py:239`)가 실제로 고정하는 것은 guard 가 아니라 `rmdir` 의
성질이라는 점은 기록해 둔다. 위 표에서 이 건은 진짜 공백 아홉에서 제외했다.

### A-R3

`tests/ai/test_amplai_kit_installer.py:38` 의
`@unittest.skipUnless(os.path.isfile(INSTALLER), "kit source is not vendored")` 는 class 전체
12 건을 가른다. manifest 확인 결과 payload 는 `tests/ai/` 세 파일을 **모든 대상 앱에 설치하지만**
`tools/amplai-loop-kit/` 는 owned_files 에 없어 함께 가지 않는다.

```text
owned_files 중 tools/ 로 시작하는 것: none
설치되는 test 파일: tests/ai/test_amplai_async_runtime.py,
                    tests/ai/test_amplai_kit_installer.py,
                    tests/ai/test_amplai_kit_regressions.py
```

결과적으로 kit 을 vendoring 하는 `amplai-foundry` 에서만 12 건이 돈다. cortex·synapse·Store 에서는
같은 파일이 조용히 전부 skip 된다. 그 앱 입장에서는 "installer test 12 건이 있다" 가 사실상
공백이다. 이 repository 안에서의 검증 공백은 아니므로 Advisory 로 둔다.

## Test Counts

두 방법으로 셌고 결과가 일치한다.

- 방법 1 — `pytest tests/ --collect-only -q` 의 파일별 수를 합산: **1502**
- 방법 2 — `pytest tests/ --collect-only` 의 요약행: `1502/1506 tests collected (4 deselected)`

주장된 1502 는 맞다. 1506 중 4 는 deselect 된다.

이번 작업의 in-scope 분은 **93** 이고, 두 방법(node id 열거 / 파일별 정의 수 grep)이 일치한다.

| 파일 | pytest | `def test_` grep |
|---|---|---|
| `tests/test_kit_distribute.py` | 21 | 21 |
| `tests/ai/test_amplai_kit_regressions.py` | 44 | 44 |
| `tests/ai/test_amplai_kit_installer.py` | 12 | 12 |
| `tests/ai/test_amplai_async_runtime.py` | 16 | 16 |
| 합계 | **93** | **93** |

증가분의 출처는 설명된다. `git diff --stat $(git merge-base main HEAD)...HEAD -- tests/` 는 파일
15 개에 12435 줄 추가를 보이고, 그중 ALR-006 이 만든 것은 위 네 파일(신규 1730 줄)이다. 나머지
증가(`test_slack_http.py` 3553 줄 등)는 이 branch 의 다른 wave 몫이며 이 lens 의 대상이 아니다.

## Skipped Tests

이 repository 에서 in-scope 93 건 중 **skip 되는 것은 없다.** `-rs` 로 확인했다.

skip 조건은 둘이고 둘 다 이 repository 에서는 성립하지 않는다.

- `tests/ai/test_amplai_kit_installer.py:38` — `skipUnless(os.path.isfile(INSTALLER))`.
  `tools/amplai-loop-kit/install.py` 가 있으므로 12 건이 모두 돈다. 배포된 앱에서의 함의는 A-R3 이다.
- `tests/test_kit_distribute.py:296` — `pytest.skip("the kit is not installed in this repository")`.
  install record 가 있으므로 돈다.

## Not Checked

- `amplai_runtime.py` 2415 줄 중 lock 계열 밖(`ProjectStore` 의 event chain, seal, 정책 계산 등).
  예산 규율에 따라 지시가 지목한 자리만 읽었다.
- `install.py` 1058 줄 중 uninstall·버전 판정 밖(fragment 병합, JSON merge, backup, rollback).
  `test_failed_registration_rolls_back_both_planes` 가 있으나 mutation 을 치지 않았다.
- `tests/ai/test_amplai_async_runtime.py` 16 건은 mutation 실행에 포함했으나 그 자체를 대상으로
  한 mutation 은 만들지 않았다. ALR-006 이 만든 것이 아니다.
- 다른 repository(cortex·synapse)에서의 실제 skip 수. 쓰지 않는다는 제약에 따라 manifest 로만
  추론했고 A-R3 의 근거는 manifest 사실이다.
- 성능·동시성의 넓은 탐색. `test_concurrent_acquire_yields_exactly_one_holder` 는 존재하지만
  반복 실행으로 flakiness 를 재지는 않았다.

## Restoration

모든 mutation 을 되돌렸다. 최종 상태를 확인했다.

```text
git status --short          (빈 출력 — 이 보고서 파일을 쓰기 전 시점)
git diff --stat             (빈 출력)
git rev-parse --short HEAD  af22ab8
seal.py --verify            ok: True
payload vs installed        NO_DRIFT
```

이후 남은 변경은 이 보고서 파일 하나뿐이다.

```text
?? specs/007-kit-source-and-distribution/evidence/review-regression.md
```

전체 suite 1502 건이 통과한다.

payload 는 mutation 대상이 아니었고(설치 사본 `scripts/` 와 `tests/ai/` 를 쳤다), `install.py` 와
`seal.py` 와 `store/supervisor/run` 은 `git checkout` 으로 되돌렸다. 따라서 seal 재실행과
재설치는 필요하지 않았고, `seal.py --verify` 와 payload/설치본 대조로 그것을 확인했다.
`~/workspace/amplai-project` 는 읽기만 했고 쓰지 않았다.
