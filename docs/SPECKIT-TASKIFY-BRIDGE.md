# Spec-Kit ↔ Taskify Bridge

`/speckit-tasks` 를 `/taskify` 로 대체한 결정([CLAUDE.md](../CLAUDE.md) "Spec-Kit Adoption")
때문에 생긴 끊긴 고리와 그 해소를 기록한다. 2026-08-03.

## The Defect

`workflow.yml` 의 `tasks` step 은 gate 로 바뀌어 `/taskify` 를 부르게 했다. 그런데 **다음 step
`implement` 는 안 바뀌었다.** `speckit.implement` 는 `tasks.md` 를 하드 요구한다:

```
.claude/skills/speckit-implement/SKILL.md
  → .specify/scripts/bash/check-prerequisites.sh --require-tasks
  → ERROR: tasks.md not found
```

`/taskify` 는 task manifest 를 만들고 `tasks.md` 는 만들지 않는다. 그래서 체인이 여기서 멈춘다.

**그리고 빠져나갈 길도 없었다** — 그 skill 의 복구 안내가 `/speckit-tasks` 를 가리켰는데,
그건 이 프로젝트가 삭제한 skill 이다. 같은 죽은 포인터가 6곳에 있었다.

이 결함은 cortex repo 에서 먼저 발견됐다 (같은 반입 결정, 같은 upstream). amplai-foundry 는
아직 speckit 체인을 한 번도 안 돌려 드러나지 않았을 뿐이었다.

## The Fix

`tasks.md` 를 manifest 에서 **생성**한다. 두 번째 source of truth 를 만들지 않는다.

```
task-manifests (source of truth)  ──generate──▶  tasks.md (목차)
  acceptance / invariants                          순서 + 파일소유 + manifest 경로
  forbidden_paths / loop
```

각 task 줄이 `manifest:` 경로를 달고 있어, 구현 주체는 계약을 YAML 에서 읽는다. 체크리스트는
순서와 파일 소유만 전달한다. `ui/dist/` 가 소스가 아니라 산출물인 것과 같은 관계다.

### 신규 — `.specify/scripts/taskify_to_tasks_md.py`

- `index.yaml` 의 wave → speckit 이 파싱하는 Phase
- `[P]` 는 **파일이 안 겹칠 때만** 붙인다. 같은 wave 라는 것만으로 병렬 안전이라고 하지 않는다
- **blocked task 는 실행 단계에서 뺀다.** 별도 "DO NOT EXECUTE" 절에 외부 의존과 함께 나열 —
  speckit 이 의존 안 풀린 일을 시작하는 걸 막는다
- 손으로 쓴 `tasks.md` 는 생성 marker 부재로 **덮어쓰기를 거부한다**
- 헤더에 **재생성 명령을 그대로 찍는다** (`--out` 포함)

### 이 repo 고유 — `--out` 이 필수다

taskify 기본 출력은 설계 소스가 `specs/` 밖이면 `.amplai/tasks/<slug>/` 다. 반면 speckit 은
FEATURE_DIR 안에서 `tasks.md` 를 찾는다. **서로 다른 디렉터리다.**

```bash
python3 .specify/scripts/taskify_to_tasks_md.py \
    .amplai/tasks/<slug> --out specs/<feature>/tasks.md
```

`--out` 없이 돌리면 `.amplai/tasks/tasks.md` 에 써서 speckit 이 못 찾는다.

### 고친 죽은 포인터

전부 `LOCAL MODIFICATION (amplai-foundry, 2026-08-03)` 주석으로 표시했다. upstream 과의 차이를
나중에 찾을 수 있다.

| 파일 | 내용 |
|---|---|
| `.specify/scripts/bash/check-prerequisites.sh:136` | 사용자가 실제로 보는 에러 문구 |
| `.claude/skills/speckit-implement/SKILL.md:180` | 복구 3단계 + "manifest 가 계약" |
| `.claude/skills/speckit-analyze/SKILL.md:60` | 전제 완화 + manifest 를 읽으라는 note |
| `.claude/skills/speckit-converge/SKILL.md:71,107` | 포인터 + **의미 충돌 경고** (아래) |
| `.specify/templates/plan-template.md:56` | 문서 트리를 생성 tasks.md 구조로 |
| `.specify/workflows/speckit/workflow.yml` | tasks gate 에 3단계 명시, implement 에 주석 |

### `/speckit-converge` 의미 충돌

이 skill 은 남은 작업을 `tasks.md` **하단에 append** 한다. 그런데 `tasks.md` 는 생성물이라
재생성하면 지워진다. skill 안에 경고를 박고 올바른 절차를 명시했다 — manifest 에 새 ID 로 쓰고,
`index.yaml` 에 추가하고, 재생성한다.

## `/grilling` — 이미 설치돼 있다 (조치 불필요)

`grill-me` 는 upstream 자체가 한 줄짜리 위임 skill 이다 (`Run a /grilling session.`). 실제
심문 protocol 은 **별도 skill `grilling`** 에 있다.

이 repo 에는 **이미 있다.** `.agents/skills/grilling` 과 `.claude/skills/grilling` 이 둘 다
HEAD 에 커밋돼 있고, tree hash 가 `10b0db61f9b3869243db8a1a0ee84f862139b94e` 로 upstream
`mattpocock/skills` 의 `skills/productivity/grilling` 과 일치한다.

protocol 요지: 질문은 한 번에 하나씩, fact 는 환경에서 직접 찾고 decision 만 사람에게 묻고,
사용자가 합의를 확인하기 전엔 실행하지 않는다.

## 안 고친 것

- `.claude/skills/speckit-specify/SKILL.md:108` — downstream 명령 나열 중 언급. 산문일 뿐
- `.specify/templates/tasks-template.md` — 삭제된 `/speckit-tasks` 전용 템플릿. 읽는 곳 없음
- `.specify/integrations/*.manifest.json` — skill 파일 해시를 들고 있고 삭제된
  `speckit-tasks/SKILL.md` 해시도 남아 있다. 이번에 고친 4개 파일 해시도 어긋난다.
  **repo 안에서 이걸 읽는 코드는 없다.** 외부 `specify` CLI 가 검증하는지는 **모른다** —
  `specify` 를 다시 돌릴 일이 생기면 그때 확인한다

## Skill 배치 규약 — symlink 와 실디렉터리가 섞여 있다

`.claude/skills/` 아래 external skill 은 대부분 **symlink** 다. 실체는 한 벌이다.

| skill | `.claude/skills/` | 실체 |
|---|---|---|
| `grill-me` | symlink (`120000`) | `.agents/skills/grill-me` |
| `taskify` | symlink (`120000`) | `.ai-team/skills/taskify` |
| **`grilling`** | **실디렉터리 (`040000`)** | `.agents/skills/grilling` 과 **각각 별개 사본** |

`grilling` 만 symlink 가 아니라 양쪽에 실파일로 들어 있다. 지금 오작동은 없으나 갱신할 때
한쪽만 바뀔 수 있다. symlink 로 통일할지는 별도 결정으로 남긴다.
