# offline_views STALE 의 구조적 원인 (r7 시도 기록)

## 시도한 것

`docs validate` 의 `offline_views` 가 STALE 인 원인을 없애려고 kit release r7 을 만들었다.

- `specs/012-portable-document-lifecycle/guide-release-r7.json` — 현재 지문으로 다시 묶은 release manifest.
  내부 정합성은 전부 통과한다 (manifest 키 8개, verification input digest, component/evidence 해시 일치, drift 0).
- `guide-verification-r7.json` — 이 tree 에서 **실제로 실행된** 증거만 참조한다:
  `s08-local-verification-r7.json` (verifier profile v2 PASS, 3174 passed),
  `s08-kit-seal-r7.json` (`seal.py --verify` ok), 그리고 r6 에서 유지되는 두 증거.

## 막힌 지점

`docs build` 가 `SOURCE_DRIFT` 로 거부한다. 원인은 문서 내용이 아니라 **review snapshot 의 구성**이다.

`scripts/amplai_docs.py:1436 review_matches` 는 저장된 review 의 `snapshot` 이 현재 document record 의
`snapshot` 과 **정확히 같을 것**을 요구한다. 그 snapshot 안에는 `candidate_revision` 이 들어 있고,
`scripts/amplai_docs.py:1367 candidate_revision` 은 문자 그대로 `git rev-parse HEAD` 다.

관측값 (docs/PORTABLE-DEVELOPMENT.md):

```
review 에 저장된 candidate_revision : a231b0ff43d3...
현재 record 의 candidate_revision   : 160b58e32aa4...
나머지 snapshot 필드                : 전부 동일
```

즉 **리뷰 이후 커밋이 하나라도 생기면** 그 리뷰는 release build 기준으로 무효가 되고
`freshness` 가 `stale` 로 떨어진다 (`amplai_docs.py:1612`). release build 는 `freshness == "verified"`
를 요구한다 (`amplai_docs.py:4177`).

## 결론

이것은 이번 Work 가 만든 회귀가 아니다. r6 도 같은 이유로 main 에서 이미 STALE 이었다.
review → build → commit 순서로 진행해도, 그 commit 자체가 HEAD 를 바꾸므로 커밋 직후 다시 stale 이 된다.
현재 규칙 아래에서 `offline_views` 를 지속적으로 green 으로 유지하는 것은 불가능하다.

## 결정이 필요한 선택지

1. **review matching 에서 `candidate_revision` 을 제외한다.** snapshot 의 나머지 필드(문서 source,
   dependency 해시)만 비교한다. 리뷰가 실제로 지키려는 것은 "검토한 내용과 현재 내용이 같은가" 이고,
   commit id 는 그 질문과 무관하다. 계약 변경이므로 Decision 이 필요하다.
2. **release build 를 tagged commit 에서만 수행한다.** 리뷰와 build 를 한 commit 안에 넣고, 그 tag 에서만
   `docs validate --repo` 를 green 으로 요구한다. 일상 개발 브랜치에서는 STALE 을 정상으로 받아들인다.
3. **현행 유지.** `offline_views` STALE 을 알려진 제약으로 문서화하고 Work 완료 판정에서 제외한다.
   지금까지 세 Work 가 실제로 이렇게 운영했다.

## 보존한 산출물

r7 manifest 와 verification 은 커밋해 둔다. 위 1번이나 2번을 택하면 그대로 쓸 수 있다.
HTML view 는 만들지 않았다 — 만들 수 없었다.
