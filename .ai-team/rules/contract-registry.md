# Public Contract / Schema Rule

## Rule

API, JSON Schema, Project Pack manifest, governance event 형식, ActionToken 계약 변경은
high risk 다. 기존 canonical 자산과 downstream consumer 의 호환성을 확인하고, generated
schema 를 수기로 고치지 않는다.

## Scope

- `schemas/**` — Pydantic model 에서 생성한 JSON Schema
- `src/amplai_foundry/**` 의 public model 과 CLI surface
- Project Pack manifest 와 `vault/projects/<project>/` 구조
- governance store migration 과 event/outbox 형식
- messenger provider 의 ingress·ack·card 계약

## Executable enforcement

- `schema`: `amplai_foundry.cli schema check` — model 과 `schemas/**` 가 어긋나면 FAIL
- `project-pack`: `amplai_foundry.cli project validate --workspace .`
- `vault-lint`: `amplai_foundry.cli lint vault/`
- `manifest-validator`: taskify task manifest 계약
- `pytest`: 계약별 test

generated schema 는 `amplai_foundry.cli schema generate` 로 다시 만든다. 손으로 편집하면
`schema check` 가 잡는다.

## Human judgement

다음은 lint 만으로 닫히지 않는다.

- 삭제·rename·semantic change 가 기존 Vault note 나 Proposal 을 무효화하는가
- schema 변경이 이미 저장된 memory object 를 읽지 못하게 만드는가
- migration 이 되돌릴 수 없는가
- messenger 계약 변경이 발송된 card 의 ActionToken 을 깨는가

이 경우 work contract 에 `public_contract` gate 를 기록하고 승인 전에는 DONE 으로 만들지
않는다. destructive migration 은 `destructive_change` gate 다.
