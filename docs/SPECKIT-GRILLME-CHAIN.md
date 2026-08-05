# Spec-Kit ↔ grill-me Chain

`/grill-me` 의 후속이 pipeline 에 정의돼 있지 않아 생긴 끊긴 고리와 그 해소를 기록한다.
2026-08-03. [SPECKIT-TASKIFY-BRIDGE.md](SPECKIT-TASKIFY-BRIDGE.md) 와 같은 계열 —
같은 반입 결정, 같은 upstream, cortex repo 에서 먼저 드러났다.

## The Defect

cortex repo 세션에서 실제로 어긋났다. 순서는 이랬다.

1. `/grill-me` 로 계획을 심문해 다듬었다 — 원인 확정, 결정 4건, 수정 파일 목록까지 나왔다
2. 사용자가 "문서부터 작성" 을 요청했다
3. `/speckit-specify` 를 부르지 않고 workstream 디렉터리에 **손으로** 분석 문서를 썼다
4. 다음 단계에서 구현 skill 을 고를 때 **"spec 은 방금 쓴 문서로 확정됐다"** 를 근거로
   `/speckit-specify` 를 배제했다

**3번이 4번의 근거가 됐다.** 스스로 만든 조건으로 speckit 을 탈락시키는 순환이다.

결과: `specs/` 가 안 생겼고, `/taskify` 와 `/speckit-implement` 가 소비할 산출물이 없어
파이프라인이 시작조차 못 했다.

## Why It Happened — 두 개의 공백

### 공백 1 — `/grill-me` 가 pipeline 에 없다

routing 표에는 있다. "계획·설계를 심문해 다듬기". 그런데 Pipeline 블록에는 없다.
그래서 **심문이 끝난 지점에서 어디로 가는지 규정이 없다.**

경계 설명은 오히려 후속을 더 열어 뒀다:

> `/grill-me` 는 형식 없는 심문이고 **아무 데나 쓴다**

"아무 데나 쓴다" 는 진입에 대한 말인데, 산출물의 출구까지 열어 둔 것으로 읽힌다.

### 공백 2 — spec 을 손으로 쓰지 말라는 규칙이 없다

`tasks.md` 에는 기계 방어가 있다. `taskify_to_tasks_md.py` 가 생성 marker 없는 파일의
덮어쓰기를 거부한다 ([SPECKIT-TASKIFY-BRIDGE.md](SPECKIT-TASKIFY-BRIDGE.md) 참조).

**`spec.md` 와 `plan.md` 에는 그런 장치가 없다.** 손으로 써도 아무도 막지 않고, 쓰고 나면
"spec 이 있다" 는 상태가 돼서 skill 을 건너뛸 명분이 생긴다.

이 repo 는 `specs/` 와 `docs/workstreams/` 가 병존한다. 둘의 역할 구분이 문서에 없으면
workstream 손문서가 spec 자리를 차지하기 쉽다.

## The Fix

`AGENTS.md` "Spec-Kit Adoption" 절 3곳을 고쳤다.

| 위치 | 변경 |
|---|---|
| Pipeline 블록 | 맨 앞에 `(/grill-me) → 계획 심문 (선택)` 추가 |
| Pipeline bullet | `/grill-me` 후속 = `/speckit-specify` 명시 |
| Pipeline bullet | spec·plan 손작성 금지 + `docs/workstreams/` = 조사 기록용 구분 |
| 경계 짝 | "아무 데나 쓴다" → 진입은 자유, **산출물은 `/speckit-specify` 로** |

역할 구분을 한 줄로 하면 이렇다.

```
docs/workstreams/  =  조사 기록  (fact 수집 · 원본 대조 · 측정 log)
specs/<feature>/   =  계약       (spec.md · plan.md · task manifest)
```

조사 기록은 손으로 쓴다. 계약은 skill 로 만든다.

## Scope — 이 repo 에서는 아직 안 터졌다

`SPECKIT-TASKIFY-BRIDGE.md` 의 결함과 같은 성격이다. cortex 에서 파이프라인을 돌리다
드러났고, amplai-foundry 는 같은 skill 세트를 쓰지만 이 경로를 아직 안 밟았다.

`skills-lock.json` 이 두 repo 에서 같은 source·같은 hash 다. 반입 결정이 같으므로
결함도 같이 온다.

## 안 고친 것

- `.specify/memory/constitution.md` (109 lines) — `grill` grep 0건이라 고칠 대상이 없다.
  `AGENTS.md` 의 파생이고 어긋나면 `AGENTS.md` 가 이긴다 (같은 절 첫 문단)
- `/grill-me` skill 파일 자체 — upstream 한 줄 위임 skill (`Run a /grilling session.`) 이고
  실제 protocol 은 `grilling` 에 있다. 후속 규정은 skill 이 아니라 이 repo 의 pipeline
  문제라 `AGENTS.md` 에만 넣었다
- cortex 쪽 `tasks.md` 생성 marker 방어의 유무 — 이 repo 에는 있다. cortex 에도 있는지는
  **모른다**. 없으면 반대 방향 반입 후보다
