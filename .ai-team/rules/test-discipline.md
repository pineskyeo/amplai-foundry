# Test Discipline Rule

## Rule

변경의 acceptance를 먼저 정의하고 실패를 재현한 뒤 최소 구현으로 통과시킨다. 테스트는
기능과 함께 유지하며, 기존 regression coverage를 이유 없이 삭제하거나 약화하지 않는다.

## Cortex-specific requirements

- functional test는 목적·입력·판정·경계를 코드에 읽을 수 있게 남긴다.
- API endpoint 추가 시 smoke test와 `docs/functional-tests.md`를 함께 갱신한다.
- algorithm 계약, `.alg` terminal/PINASSIGN/pin-map, profile generation 경로 변경은
  `make functional-test`를 반드시 verification에 포함한다.
- `make test-ci`와 converter test만 green이어도 profile generation의 silent-wrong을 막았다고
  단정하지 않는다.
- manual check는 owner, 절차, expected result를 적고 automated PASS로 위장하지 않는다.

## Executable enforcement

- `profile-functional-guard`
- `build-test-ci`
- `functional`
- `smoke`
- change-specific slice Eval command
