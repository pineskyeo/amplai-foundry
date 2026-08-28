# Project Store 가 Supervisor 를 소유한다

> **SUPERSEDED by `specs/007-kit-source-and-distribution` (`ALR-006`, `D-053`).**
> S01·S02 가 kit 2.3.0 기능으로 흡수됐다 — kit 정본을 amplai-foundry 가 가지면
> 단일 인스턴스 락을 `amplai_supervisor.py` 안에 넣을 수 있고, 이 문서가 한계로
> 적은 우회 경로가 사라진다. **`D-052` 의 실질(Store 가 supervisor 를 소유한다)은
> 그대로 유효하고** `D-053` 이 amend 했다. 이 문서는 그 경위의 기록으로 남긴다.

`ALR-005`. `D-052` 가 승인한 구조를 실행 가능한 형태로 적는다.

## What

Local Supervisor 의 **코드와 실행 권한을 Project Store 로 옮긴다.**

```text
<PROJECT_HOME>/                    Store (git repo)
├── project.json, policy.json      kit 소유 — 안 건드린다
├── apps/, contracts/, changes/    kit 소유 — 안 건드린다
├── .amplai/locks/                 host-local (Store .gitignore 가 제외한다)
│   ├── project.lock               kit
│   └── supervisor.lock            신규 — 단일 인스턴스를 여기서 강제한다
└── supervisor/                    신규 — kit 이 만들지 않는 영역
    ├── amplai_supervisor.py       amplai-foundry 사본을 동기화
    ├── amplai_runtime.py          같음 (supervisor 가 import 한다)
    ├── VERSION                    동기화된 kit 버전
    └── run                        진입점. 락을 잡고 supervisor 를 부른다
```

**소유는 Store, 공급은 amplai-foundry 다.**

## Why

supervisor 는 **개념상 이미 앱의 물건이 아니다.** kit 의 아키텍처 그림이 Store 아래 별도
층으로 그리고, 코드도 `--app` 없이 `list_apps()` 로 등록된 앱 전부를 순회한다.

그런데 kit 이 `owned_files` 로 **모든 앱 repo 에 같은 파일을 복사한다.** 앱 단위 설치 도구라
코드를 놓을 자리가 앱 밖에 없기 때문이다. 어느 것을 돌려야 하는지는 아무 문서도 안 정한다.

그리고 **단일 인스턴스 보장이 전혀 없다.** `grep "lock|pid|flock|singleton"` 이 0건이다.
`claim_work` 이 Store 락 안에서 `max_concurrency` 를 재검사하므로 같은 Work 의 중복 실행은
막히지만, supervisor 프로세스가 둘 뜨는 것은 아무것도 막지 않는다.

## Constraints

- **kit 원본을 고치지 않는다.** Store 쪽에만 만든다. synapse 의 PR #81 과 독립적으로
  진행할 수 있어야 한다
- **kit 이 소유한 Store 경로를 건드리지 않는다** — `project.json`, `policy.json`, `apps/`,
  `contracts/`, `changes/`, `.amplai/local/`. 새로 쓰는 것은 `supervisor/` 와
  `.amplai/locks/supervisor.lock` 뿐이다
- **버전 드리프트는 fail-closed 로 막는다.** 조용히 도는 것보다 안 도는 것이 낫다
- **우회 경로를 숨기지 않는다.** 앱 repo 의 supervisor 직접 실행은 코드로 못 막는다.
  문서에 적고 upstream 제안으로 올린다
- Store 가 아직 없다. 이 feature 가 Store 생성을 포함한다

## Non-goals

- **supervisor 를 켜는 것** (`--run`). HANDOFF §3 이 활성 세션 라우팅 확인 전까지 쓰지
  말라 했고 그 판단은 그대로다. 켜지 않아도 이 구조는 필요하다
- HANDOFF §3 의 개선 후보 1(worktree 인식)·2(알림)·3(활성 세션 라우팅) 설계
- kit 원본에 락을 넣는 것 — upstream 제안으로만 올린다
- 앱 repo 에서 `scripts/amplai_supervisor.py` 를 제거하는 것 — kit 재설치가 되돌린다

## Acceptance

| id | 내용 |
|---|---|
| AC-001 | Project Store 가 `AMPLAI_PROJECT_HOME` 아래 생성돼 있고 `project verify` 가 통과한다 |
| AC-002 | `<PROJECT_HOME>/supervisor/` 에 `amplai_supervisor.py`·`amplai_runtime.py`·`VERSION`·`run` 이 있고, 두 `.py` 가 amplai-foundry 사본과 **byte 동일**하다 |
| AC-003 | `run` 이 `.amplai/locks/supervisor.lock` 을 잡는다. **두 번째 실행이 거부되고 exit code 로 구분된다.** 첫 번째가 죽으면 락이 회수돼 다음 실행이 성공한다 |
| AC-004 | `supervisor/VERSION` 과 등록된 각 앱의 `.ai-team/install/amplai-loop-kit.json` 버전이 다르면 `run` 이 **fail-closed** 한다. 어느 앱이 어긋났는지 출력한다 |
| AC-005 | 동기화 명령이 amplai-foundry 에 있고, 실행하면 `supervisor/` 의 두 `.py` 와 `VERSION` 이 갱신된다. 갱신 전후로 AC-002 가 유지된다 |
| AC-006 | `run --dry-run` 이 kit 의 `--dry-run` 을 그대로 통과시키고 Work 를 claim 하지 않는다 |
| AC-007 | kit 이 소유한 Store 경로(`project.json`, `policy.json`, `apps/`, `contracts/`, `changes/`, `.amplai/local/`)가 **이 feature 의 어떤 동작으로도 안 바뀐다** |
| AC-008 | 우회 경로(앱 repo 의 `scripts/amplai_supervisor.py` 직접 실행)가 락을 잡지 않는다는 사실이 문서에 적혀 있고, 그것이 재현으로 확인됐다 |
| AC-009 | 제거 절차가 있다 — `supervisor/` 와 락을 지우면 Store 가 kit 이 만든 상태로 돌아간다. 복제 Store 에서 확인한다 |

## Slices

```text
S01  Store 생성 + supervisor/ 배치 + 동기화 명령        (AC-001, 002, 005, 007)
S02  run 진입점 — 단일 인스턴스 락 + 버전 대조          (AC-003, 004, 006)
S03  우회 경로 기록 + 제거 절차 + upstream 제안 초안    (AC-008, 009)
```

## Risks

- **버전 드리프트.** `amplai_supervisor.py` 가 같은 디렉토리의 `amplai_runtime.py` 를
  import 하므로(`sys.path` 에 `SCRIPT_DIR` 삽입) 사본이 둘이 된다. AC-004 의 fail-closed 가
  방어책이고, 그것이 동작하는지가 이 feature 의 핵심 검증이다
- **우회 경로.** kit 원본을 안 고치므로 앱 repo 사본 직접 실행을 막을 수 없다. 규약과
  문서로만 막힌다 — **한계를 명시한다**
- **Store 가 아직 없다.** synapse 도 `--project-home` 없이 설치했다. Store 를 처음 만드는
  것이 이 feature 의 일부이고, 그 시점에 등록할 앱을 정해야 한다
- **`D-051` 의 2.2.0 반영이 보류이고 설치도 미실행이다.** amplai-foundry 에 kit 이 아직
  안 깔렸다. Store 의 `supervisor/` 에 놓을 코드를 어디서 가져올지가 거기 걸린다 (아래 참조)

## Dependency — `D-051` 과의 관계

`D-052` 는 Store 구조를 정하고 `D-051` 은 amplai-foundry 에 kit 을 설치할지를 정한다.
**`D-051` 자체는 APPROVED 이지만 두 가지가 미완이다** — 2.2.0 반영(전제 넷이 무효화됐다)이
보류이고, 설치도 아직 실행되지 않았다 (`specs/005-amplai-loop-kit` 이 `ready` 상태).

`supervisor/` 에 놓을 코드의 정본이 amplai-foundry 사본이므로 `D-051` 이 정해지기 전에는
S01 을 완주할 수 없다. 두 경로가 있다.

```text
경로 A   D-051 을 2.2.0 기준으로 amend → amplai-foundry 설치 → 그 사본을 정본으로
경로 B   synapse 의 tools/amplai-loop-kit/payload 를 정본으로 삼고 amplai-foundry 설치와
         분리한다 — 이 경우 "공급은 amplai-foundry" 가 성립하지 않는다
```

**경로 A 가 `D-052` 의 서술과 맞는다.** 착수 전에 `D-051` 을 정해야 한다.

## Source

- `D-052` — 이 feature 를 승인한 Decision
- `HANDOFF_2026-08-28_amplai-loop-kit-2.2.0.md` §3 — supervisor 설계 과제
- kit 2.2.0 `scripts/amplai_supervisor.py`, `amplai_runtime.py`, `manifest.json`
- `.ai-team/runtime/LOCAL_SUPERVISOR.md` — supervisor 책임 정의
- `D-051` — kit 설치 결정 (APPROVED. 2.2.0 반영 보류 + 설치 미실행)
