# Commit / Release Rule

## Rule

commit 또는 release artifact는 변경 내용과 검증 evidence가 일치해야 한다. source만 바꾸고
tracked generated artifact, test, 문서, RPM manifest를 stale 상태로 남기지 않는다.

## Local commit gate

```bash
make hooks
```

`.githooks/commit-msg`는 Loop Runtime `commit` verifier profile을 실행한다.

- fast static invariants
- staged high-risk path의 `[contract-ok]` acknowledgement
- `[skip-checks]` 또는 `--no-verify`는 명시적 우회이며 검증을 완료했다는 뜻이 아니다

CI는 message context가 없는 `fast` profile을 독립적으로 다시 실행한다.

## Heavy evidence

변경 범위에 따라 `/work` contract가 `standard`, `runtime`, `full`, `rhel` profile을 선택한다.
release/production 영향이 있으면 최소한 다음을 확인한다.

- fresh build/test 결과
- `cortex.spec` version/`%files` 영향
- UI 변경이면 rebuilt `ui/dist/`
- public contract/schema/registry compatibility
- 필요한 production operation human gate

commit, push, PR, merge, release는 사용자 명시 요청 전에는 자동 수행하지 않는다.
