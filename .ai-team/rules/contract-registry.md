# Public Contract / Registry Rule

## Rule

설비 배포와 recipe 생성에 영향을 주는 API, schema, registry, ID mapping, algorithm interface,
service/spec 변경은 high risk다. 기존 production artifact와 downstream consumer의 호환성을
확인하고, generated registry를 수기로 고치지 않는다.

## Scope

- `conf/testplan/**`
- `docs/**/schemas/**`, `**/*.schema.json`
- public headers와 ABI
- `sdk/algorithms/**`, `sdk/authoring/**`
- `cortex.spec`, service/sudoers/deployment config

## Executable enforcement

- `testplan-refs`: condition/algorithm ID referential integrity
- `algorithm-alg-parity`: runtime와 authoring `.alg` twin parity
- `algorithm-alg-blankline`: canonical `.alg` layout
- `schemas`: schema/sample compatibility
- `high-risk-ack`: pending commit message acknowledgement

## Human judgement

다음은 lint만으로 닫히지 않는다.

- 삭제·rename·semantic change가 downstream artifact를 무효화하는가
- ABI/API/schema/file format compatibility를 깨는가
- `%files` 또는 service 변경이 설비 배포분을 바꾸는가
- registry가 정식 generator pipeline에서 재생성됐는가

이 경우 work contract에 `public_contract` gate를 기록하고 승인 전에는 DONE으로 만들지 않는다.
