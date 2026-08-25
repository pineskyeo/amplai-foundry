# Test Discipline Rule

## Rule

변경의 acceptance 를 먼저 정의하고 실패를 재현한 뒤 최소 구현으로 통과시킨다. test 는
기능과 함께 유지하며, 기존 regression coverage 를 이유 없이 삭제하거나 약화하지 않는다.

## 이 저장소의 요구

- test 는 `.venv/bin/python -m pytest` 로 돌린다. 맨 `python3` 는 system 3.9 라
  `pyproject.toml` 의 `requires-python = ">=3.11"` 을 만족하지 않고 엉뚱한 import 실패를 낸다.
- 기본 실행은 `slack_e2e` marker 를 deselect 한다 (`pyproject.toml` `addopts`). 실제 Slack
  workspace 를 치는 test 는 `-m slack_e2e` 로만 돈다. `verify` 를 network 의존으로 만들지
  않는다.
- governance store·ingress·outbox 계약을 바꾸면 migration test 와 replay/idempotency test 를
  같은 변경에 포함한다.
- messenger provider 를 추가하거나 고치면 다른 provider 의 activation 이 안 바뀌는 것을
  test 로 남긴다.
- 계약 test 는 목적·입력·판정·경계를 코드에서 읽을 수 있게 남긴다.
- manual check 는 owner, 절차, expected result 를 적고 automated PASS 로 위장하지 않는다.

## Executable enforcement

- `pytest`: 전체 suite. `standard` 이상 profile 에 들어간다
- `amplai-foundry verify`: pytest·ruff check/format·mypy·schema·vault-lint·project-pack 7 check
- slice 별 Eval command: `scripts/eval.sh --feature specs/<feature> --slice <id>`

실행하지 않은 검증을 통과했다고 쓰지 않는다. 실패는 명령, exit code, 원인과 함께 남긴다.
