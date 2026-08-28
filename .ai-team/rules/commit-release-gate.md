# Commit / Release Rule

## Rule

commit 은 변경 내용과 검증 evidence 가 일치해야 한다. source 만 바꾸고 test, 문서, schema,
Work artifact 를 stale 상태로 남기지 않는다.

## Commit gate

이 저장소에 git hook 은 없다. gate 는 verifier profile 을 직접 돌려서 연다.

```bash
.venv/bin/python .ai-team/verifiers/run.py --profile commit
```

`commit` profile 은 `fast`(ruff check/format, mypy, loop runtime doctor, task manifest
validator)에 `git diff --check` 를 더한다. `high` risk 경로가 staged 면 `/work` contract 가
`v2` 를 요구한다 — `commit` profile 만으로 그 경로를 닫지 않는다.

`--no-verify` 는 명시적 우회이며 검증을 완료했다는 뜻이 아니다.

## Heavy evidence

변경 범위에 따라 `/work` contract 가 `standard`, `runtime`, `full`, `v2` profile 을 선택한다.
`profile_rank` 가 그 상대 강도를 정하고, 이름의 정본은 `.ai-team/verifiers/registry.json` 의
`profiles` 다.

public contract 영향이 있으면 최소한 다음을 확인한다.

- `schemas/**` 와 Pydantic model 의 일치 — `amplai_foundry.cli schema check`
- Project Pack manifest 호환 — `amplai_foundry.cli project validate --workspace .`
- Vault 지식 lifecycle — `amplai_foundry.cli lint vault/`
- 필요한 human gate 승인

## Publish

commit, push, PR, merge, release 는 사용자 명시 요청 전에는 자동 수행하지 않는다.
`.ai-team/policy/permissions.json` 이 `commit` 을 `git_publish` gate 로 두고 `push`·`merge`·
`release`·`deploy` 를 `prohibited` 로 둔다.
