# Plan — Kit 정본 이관과 배포

`ALR-006`. `spec.md` 의 WHAT/WHY 를 어떻게 만들지 적는다. 구현하지 않는다.

## 1. 경로 설정 — 두 층으로 나눈다

사용자가 명시적으로 요구한 부분이다. **"어느 경로에 어떻게 파일을 전달하는지" 를 설정으로
분리한다.**

### 왜 두 층인가

kit 자신이 이미 그렇게 한다.

```text
apps/<app_id>.json                   커밋   논리 정보 (app_id, max_concurrency, ...)
.amplai/local/apps/<app_id>.json     로컬   repo_path 같은 절대경로
```

절대경로를 커밋하면 다른 머신·다른 사용자에서 깨진다. **같은 패턴을 따른다.**

### 층 1 — `tools/amplai-loop-kit/distribution/targets.json` (커밋)

```json
{
  "schema_version": "1.0",
  "project_id": "ai-platform",
  "targets": [
    {
      "app_id": "amplai-foundry",
      "role": "source",
      "path_hint": ".",
      "notes": "kit 정본을 소유한다. 자기 자신에도 설치한다"
    },
    {
      "app_id": "synapse",
      "role": "target",
      "path_hint": "../synapse",
      "notes": "2.2.0 이 이미 설치돼 있다. 2.3.0 으로 갱신한다"
    },
    {
      "app_id": "cortex",
      "role": "target",
      "path_hint": "../cortex",
      "notes": "신규 설치"
    }
  ]
}
```

- `path_hint` 는 **힌트일 뿐 권위가 없다.** 실제 경로는 층 2 가 정한다
- `role` 은 `source` 하나와 `target` 여럿이다. `source` 는 kit 정본을 소유하는 앱이고
  배포 대상이기도 하다
- `project_id` 는 Store 등록에 쓴다. 세 앱이 같은 Store 를 공유한다

### 층 2 — `.ai-team/local/kit-targets.json` (host-local, gitignore)

```json
{
  "schema_version": "1.0",
  "project_home": "/Users/pinesky/workspace/amplai-project",
  "paths": {
    "amplai-foundry": "/Users/pinesky/workspace/amplai-foundry",
    "synapse": "/Users/pinesky/workspace/synapse",
    "cortex": "/Users/pinesky/workspace/cortex"
  }
}
```

- `.gitignore` 에 `.ai-team/local/` 을 넣는다. **kit 이 이미 그 줄을 marker 로 넣으므로
  중복되지 않게 확인한다** (`fragments/gitignore.md` 가 `.ai-team/local/` 을 담는다)
- 파일이 없으면 `path_hint` 로 후보를 만들어 **사람에게 확인을 요청하고 멈춘다.**
  추측한 경로로 남의 저장소에 쓰지 않는다
- `~` 와 `$VAR` 를 확장한다. 확장 뒤 절대경로가 아니면 거부한다

### 해석 순서

```text
1. --app <id> 로 지정했으면 그 하나만
2. .ai-team/local/kit-targets.json 의 paths[app_id]
3. 없으면 targets.json 의 path_hint 를 정본 위치 기준으로 해석해 후보로 제시하고 멈춘다
```

## 2. 배포 래퍼 — `scripts/kit_distribute.py`

`install.py --target` 이 경로를 하나만 받으므로(`install.py:894`) 래퍼가 순회한다.

```bash
scripts/kit_distribute.py --dry-run           # 전체 계획. 아무것도 안 바꾼다
scripts/kit_distribute.py --all               # 전체 배포
scripts/kit_distribute.py --app cortex        # 하나만
scripts/kit_distribute.py --verify            # 설치된 버전을 kit VERSION 과 대조
scripts/kit_distribute.py --uninstall --app cortex
```

### 배포 절차

```text
1. preflight
   - selftest.py 통과            (kit 패키지 무결성)
   - CHECKSUMS.sha256 대조
   - 대상 경로 해석 — 하나라도 못 정하면 여기서 멈춘다
   - 각 대상이 git repo 이고 working tree 가 깨끗한지 확인
2. 전체 dry-run
   - 대상마다 install.py --dry-run
   - **하나라도 실패하면 아무것도 설치하지 않고 끝낸다**
3. 순차 설치
   - source(amplai-foundry) 를 먼저, 그다음 target 을 targets.json 순서로
   - 실패하면 그 지점에서 멈추고 무엇까지 됐는지 보고한다
   - 이미 설치된 앱을 되돌리지 않는다 — kit 이 앱별로 rollback 한다
4. 사후 검증
   - 각 대상의 .ai-team/install/amplai-loop-kit.json 버전이 kit VERSION 과 같은지
   - Store 가 있으면 amplai.py project verify
```

**source 를 먼저 설치하는 이유**는 그 앱이 kit 정본을 갖고 있어 실패 시 진단이 가장 쉽고,
Store 의 `supervisor/` 가 그 사본을 정본으로 삼기 때문이다 (`D-052`).

### 출력

각 단계는 기계가 읽을 수 있는 JSON 한 덩어리로 낸다. `install.py` 의 출력 형식을 따른다.

## 3. supervisor 를 kit 안으로

`D-052` 가 정한 구조를 `install.py` 가 만든다.

```text
<PROJECT_HOME>/
├── supervisor/
│   ├── run                 진입점
│   └── VERSION             설치된 kit 버전
└── .amplai/locks/
    └── supervisor.lock     단일 인스턴스
```

### 락을 어디서 잡나 — kit 안이다

`D-052` 는 Store 래퍼에서 잡기로 했고 **우회 경로를 한계로 인정했다.** 정본을 가지면
`amplai_supervisor.py` **자체가** 락을 잡을 수 있고 그 한계가 사라진다.

```text
D-052 (원안)   Store 의 run 이 락을 잡는다 → 앱 사본 직접 실행은 우회한다
D-053 (개정)   amplai_supervisor.py 가 락을 잡는다 → 어느 사본을 실행해도 막힌다
```

락은 `discover_project_home()` 이 찾은 Store 아래에 잡는다. Store 를 못 찾으면 supervisor
자체가 동작할 수 없으므로 그 경우는 기존대로 실패한다.

### 락 성질

- **host-local 이다.** Store `.gitignore` 가 `.amplai/locks/` 를 제외한다
- **stale 회수가 필요하다.** 프로세스가 죽으면 락이 남는다. kit 의 project lock 이 2.2.0
  에서 `rename` 하나로 단독 승자를 만드는 방식을 쓰므로 **같은 방식을 따른다** (C1 수정)
- 두 번째 실행은 **거부되고 exit code 로 구분된다.** 조용히 대기하지 않는다

### runtime 사본 문제가 사라진다

`D-052` 는 Store 에 `amplai_runtime.py` 사본을 두려 했고 그것이 드리프트 위험이었다.
락이 kit 안으로 들어가면 **Store 에 둘 것은 `run` 진입점과 `VERSION` 뿐**이고 실행은 앱
사본을 쓴다. 사본이 하나로 줄어 드리프트가 없어진다.

`run` 은 `kit-targets.json` 의 `paths["amplai-foundry"]` 를 읽어 그 앱의
`scripts/amplai_supervisor.py` 를 부른다. 버전은 `supervisor/VERSION` 과 그 앱의 install
record 를 대조해 확인한다.

## 4. 공용화 — 무엇을 고치나

실측한 synapse 언급 56건을 분류했다.

| 대상 | 건수 | 처리 |
|---|---|---|
| `payload/tests/ai/*.py` | 36 | **안 고친다.** cross-app 시나리오의 fixture 앱 이름이다. 편향이 아니라 예시 데이터다 |
| `README.md` | 8 | 첫 줄 "Synapse의 AMPLAI Loop V2를 기준으로" 를 중립으로. 설치 예시 경로도 |
| `reference/TEST_REPORT.md` | 6 | 시험 기록이다. 각주로 출처를 남기고 본문은 보존한다 |
| `reference/SYNAPSE_INTEGRATION.md` | 2 | **파일 이름을 바꾼다** — `REFERENCE_INTEGRATION.md`. 내용의 "Synapse repository" 를 "reference Loop V2 app" 으로 |
| `examples/PROJECT_STORE_QUICKSTART.md` | 2 | 예시 경로 중립화 |
| `payload/.ai-team/runtime/INSTALLATION.md` | 1 | 예시 `--id synapse` → `--id <app-id>` |
| `CHECKSUMS.sha256` | 1 | 파일명 변경에 따라 재생성 |

**marker 대상 셋**(`handoff` 둘, `AUTONOMY_POLICY.md`)은 지우지 않는다. `required: false`
라 없는 앱에서도 설치되고, `handoff` skill 이 있는 앱에서는 유용하다. 대신 **왜 optional
인지 `manifest.json` 주석이나 README 에 명시**한다 (AC-004).

## 5. 마이그레이션 — synapse 는 이미 2.2.0 이다

```text
amplai-foundry   미설치 → 2.3.0 신규
cortex           미설치 → 2.3.0 신규
synapse          2.2.0  → 2.3.0 갱신 (idempotent update 경로)
```

synapse 만 갱신 경로를 탄다. kit 이 owned-file hash 로 로컬 수정을 감지해 충돌 시
멈추므로, **synapse 에서 kit 설치물을 손댔는지 먼저 확인해야 한다.**

그리고 synapse `tools/amplai-loop-kit/` 삭제는 별도 PR 이다. 삭제 전에 그 저장소에서
kit 을 참조하는 자리를 전수로 센다 (AC-013).

## 6. 실패 경로

| 실패 | 처리 |
|---|---|
| 대상 경로를 못 정한다 | preflight 에서 멈춘다. 추측한 경로로 쓰지 않는다 |
| 대상 working tree 가 더럽다 | 그 대상을 건너뛰고 보고한다. 사람이 정리한 뒤 다시 돌린다 |
| dry-run 이 하나라도 실패 | **아무것도 설치하지 않는다** |
| 설치 중 실패 | 그 지점에서 멈춘다. kit 이 그 앱을 rollback 한다. 앞선 앱은 그대로 둔다 |
| 버전 다운그레이드 | kit 이 거부한다 (2.2.0 이 추가한 기능) |
| Store 가 없다 | supervisor 설치를 건너뛰고 그 사실을 보고한다. 앱 설치는 진행한다 |

## 7. 검증

```text
kit 패키지     selftest.py, CHECKSUMS 대조
이 저장소      loopctl doctor, verifier --profile v2, pytest
cortex         그 저장소의 doctor
synapse        그 저장소의 doctor + 기존 tests/ai 통과
Store          amplai.py project verify
락             동시 실행 거부 + SIGKILL 후 회수 + 앱 사본 직접 실행도 거부
배포           --verify 가 세 앱의 install record 를 kit VERSION 과 대조
```

**락 검증이 이 feature 의 핵심이다.** `D-052` 가 한계로 적은 우회 경로가 실제로 닫혔는지
재현으로 확인한다 (AC-007).
