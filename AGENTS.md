# Repository Rules

이 저장소는 AMPLAI V3 다. 자연어 목표를 승인된 계약으로 바꾸고, 격리된 sandbox 에서 실행하고,
독립 verifier 가 검증한 결과만 draft PR 로 돌려주는 runtime 과, 그 harness 를 증거로 개선하는
meta-harness 를 담는다.

## Fixed Inputs

- `design-reference/` 는 V3 설계 정본이다. 고치지 않는다.
- `src/amplai_foundry/runtime/contracts/data/` 는 frozen 3.0.0 schema·계약이다. 고치지 않는다.
- 결정 기록은 `docs/workstreams/v3-real-execution/DECISIONS.md` 에 남긴다. 과거 결정은 고치지 않고
  새 결정으로 대체한다.

## Development Entry

개발 작업은 `amplai` CLI 로 한다. 사용법은
[docs/v3/USING_AMPLAI_WORK.ko.md](docs/v3/USING_AMPLAI_WORK.ko.md) 가 정본이다.

```text
amplai work "<goal>"      → 계약 초안 → amplai approve <goal-id> → 검증된 draft PR
amplai status | steer | replan | cancel
amplai design "<problem>" → 설계 문서만, 구현·배포 없음
amplai ops ...            → local-init, local-add-app, local-verifier, local-serve 등 운영
amplai meta ...           → meta-harness gate (propose, screen, experiment, canary, promote)
```

사람 승인이 필요한 gate(계약 승인, focused·holdout 실험, 배포)는 agent 가 대신 실행하지 않는다.

보조 skill(`eli12`, `grill-me`, `grilling`)은 loop 밖에 있고 사용자가 직접 부른다. 정본은
`.agents/skills/` 이고 `.claude/skills/<name>` 은 그것을 가리키는 symlink 다.

## Change Scope

- 작은 검증 가능한 변경을 우선한다.

## Completion Gate

- 변경한 계약에 맞는 test 를 추가한다.
- 다음 네 가지가 통과해야 완료로 보고한다 (`.github/workflows/ci.yml` 과 같다).

  ```bash
  .venv/bin/python -m pytest -n auto --dist worksteal
  .venv/bin/ruff check .
  .venv/bin/ruff format --check .
  .venv/bin/mypy src
  ```

- 실행하지 않은 검증을 통과했다고 보고하지 않는다.
- 실패를 숨기지 않고 명령, exit code 와 원인을 기록한다.

## Review Before Merge

관점이 서로 다른 독립 reviewer 셋(contract, failure/recovery, regression)을 **순차로** 돌린다.
동시에 띄우면 세션 한도로 셋 다 잃는다. 각 reviewer 에게 큰 파일을 통째로 읽지 말라는 예산
규율을 준다. P0·P1·Blocking-P2 가 하나라도 있으면 merge 하지 않는다.
